#!/usr/bin/env python3


# test for group3: "pruning and training the network"

# The test integrate separate passes of pruning, quantization, training, and Huffman coding

import logging
import os
import sys
from pathlib import Path

import toml
import torch
import pytorch_lightning as pl
import pdb

# Housekeeping -------------------------------------------------------------------------
os.environ["PYTHONBREAKPOINT"] = "ipdb.set_trace"
sys.path.append(Path(__file__).resolve().parents[5].as_posix())

import chop.models as models
from chop.passes.graph import (
    add_common_metadata_analysis_pass,
    init_metadata_analysis_pass,
    add_software_metadata_analysis_pass,
    profile_statistics_analysis_pass,
    prune_transform_pass,
)
from chop.passes.graph.analysis.pruning.calculate_sparsity import (
    add_pruning_metadata_analysis_pass,
)

from chop.passes.graph import PASSES
from chop.ir.graph.mase_graph import MaseGraph
from chop.tools.get_input import InputGenerator, get_dummy_input
from chop.dataset import MaseDataModule, get_dataset_info
from chop.tools.logger import set_logging_verbosity
import pprint
from chop.passes.graph.utils import get_node_actual_target
import os

set_logging_verbosity("debug")

logger = logging.getLogger("chop.test")

from chop.passes.graph.interface import (
    load_mase_graph_interface_pass,
    save_mase_graph_interface_pass,
)
from chop.passes.graph.utils import deepcopy_mase_graph
from chop.tools.checkpoint_load import load_model
from chop.tools.config_load import load_config
from chop.tools.get_input import InputGenerator, get_cf_args, get_dummy_input
from chop.tools.utils import parse_accelerator, to_numpy_if_tensor

from chop.passes.graph.transforms import metadata_value_type_cast_transform_pass

from chop.passes.graph.transforms.pruning.pruning_methods import (
    weight_criteria_map,
    activation_criteria_map,
)

from chop.passes.graph.transforms import metadata_value_type_cast_transform_pass
from chop.passes.graph.utils import get_mase_op, get_mase_type, get_node_actual_target
from chop.plt_wrapper import get_model_wrapper
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint

import copy

pp = pprint.PrettyPrinter(indent=4)

if "test" in os.getcwd():  # if in "mase/machop/test/passes/graph/transforms/prune"
    config_file = "../../../../../configs/examples/group3.toml"
elif "machop" in os.getcwd():  # if in "mase/machop"
    config_file = "configs/examples/group3.toml"
elif "mase" in os.getcwd():  # if in "mase"
    config_file = "machop/configs/examples/group3.toml"


def pre_transform_load(
    model_short_name,
    mask,
    is_quantize,
    load_name: str,
    load_type: str,
    model: torch.nn.Module,
):
    if load_name is not None and load_type in ["pt", "pl"]:
        model = load_model(
            model_short_name,
            mask,
            is_quantize,
            load_name=load_name,
            load_type=load_type,
            model=model,
        )
        """
        load_model is added with parameters of 'mask' and 'is_quantize';
        1. after pruning, there should be a group of new keys in state_dict(), all with "mask"
        e.g. state_dict['feature_layers.0.parametrizations.weight.0.mask'] = mask[0]
        2. if quantized, the weight parameters will have parametrizations;
        e.g. change from 'feature_layers.0.weight' to 'feature_layers.0.parametrizations.weight.original'
        """
    return model


"""
model storage size (Bytes)
"""


def model_storage_size(model, is_quantize, dict_weight_masks):
    """
    if quantize, use 8 bits for each parameters
    else, use 32 bits
    """
    if is_quantize:
        weight_bit_width = 8
        bias_bit_width = 8
    else:
        weight_bit_width = 32
        bias_bit_width = 32
    total_bits = 0
    for name, module in model.named_modules():
        if isinstance(module, torch.nn.Conv2d):  # we only focus on Conv2d
            if dict_weight_masks != {}:  # if pruned
                if hasattr(module, "weight"):
                    name = name.rsplit(".")
                    name = "_".join(name)
                    mask = dict_weight_masks[name]
                    if mask is not None:
                        num_unpruned = torch.sum(mask).item()
                        bits = num_unpruned * weight_bit_width
            else:  # if not pruned
                bits = module.weight.numel() * weight_bit_width
            total_bits += bits
        if hasattr(module, "bias") and module.bias is not None:  # bias
            bits = module.bias.numel() * bias_bit_width
            total_bits += bits
    total_bytes = total_bits / 8
    return total_bytes


"""
number of FLOPs (only for Conv2d)
"""


def conv_flop(model, act_masks, dict_weight_masks):
    """
    FLOP: K * K * H_out * W_out * C_out * C_in * 2
    """
    conv_flop_before_prune = 0
    conv_flop_after_prune = 0
    assert len(act_masks) == len(
        dict_weight_masks.keys()
    ), "do not have the same number of elements within activation masks and weight masks, please check"
    i = 0
    for name, module in model.named_modules():
        if isinstance(module, torch.nn.Conv2d):
            H_out = act_masks[i].shape[-2]
            W_out = act_masks[i].shape[-1]
            name = name.rsplit(".")
            name = "_".join(name)
            C_out = dict_weight_masks[name].shape[0]
            C_in = dict_weight_masks[name].shape[1]
            K = dict_weight_masks[name].shape[2]
            conv_flop_before_prune += (K * K * H_out * W_out) * C_in * C_out * 2
            # prune
            remain_percent = torch.sum(act_masks[i] > 0) / (
                act_masks[i].shape[0]
                * act_masks[i].shape[1]
                * act_masks[i].shape[2]
                * act_masks[i].shape[3]
            )
            conv_flop_after_prune += int(
                (K * K * H_out * W_out) * (C_in * remain_percent) * C_out * 2
            )
            i += 1
    return conv_flop_before_prune, conv_flop_after_prune


def run(config_file):
    BATCH_SIZE = 512
    root = Path(__file__).resolve().parents[5]
    config = toml.load(open(config_file))
    with open(config_file) as f:
        config = toml.load(f)
        print("config: ")
        print(config)

    model_name = config["model"]
    dataset_name = config["dataset"]

    load_name = config["passes"]["retrain"]["load_name"]
    # load_name = None    #  Set load_name to None if want to train from scratch
    load_type = config["passes"]["retrain"]["load_type"]
    accelerator = config["passes"]["retrain"]["trainer"]["accelerator"]
    task = config["task"]
    accelerator = parse_accelerator(accelerator)

    if "content" not in load_name:
        load_name = None

    """
    model_short_name: choose from vgg7 / resnet18
    daatset_short_name: choose from cifar10 / mnist (colored-MNIST in essence)
    """
    model_short_name = config["model"]
    print("model used: ", model_short_name)
    dataset_short_name = config["dataset"]
    print("dataset used: ", dataset_short_name)

    # if has pre-trained model, then load
    dataset_info = get_dataset_info(dataset_name)
    model = models.get_model(
        name=model_short_name,
        task=task,
        dataset_info=dataset_info,
        pretrained=False,
        checkpoint=None,
        quant_config=None,
    )
    weight_masks = (
        None  # mask generated by weight pruning, will be assigned value later
    )
    is_quantize = False
    """
    if "vgg7 + cifar10": fine-tune pretrained model / train from scratch
    if not, train from scratch
    """
    model = pre_transform_load(
        model_short_name,
        weight_masks,
        is_quantize,
        load_name=load_name,
        load_type=load_type,
        model=model,
    )
    model.to(accelerator)

    """
    Pruning, Quantization, Training all need to save models when finished
    """
    save_dir = f"../mase_output/group3_test/"
    prune_save_dir = os.path.join(save_dir, "prune")
    quantize_save_dir = os.path.join(save_dir, "quantize")
    retrain_save_dir = os.path.join(save_dir, "retrain")

    # concrete forward args for freezing dynamic control flow in forward pass
    model_info = models.get_model_info(model_short_name)
    if "cf_args" not in config:
        cf_args = get_cf_args(model_info=model_info, task=task, model=model)
    else:
        cf_args = config["cf_args"]

    # graph generation
    graph = MaseGraph(model=model, cf_args=cf_args)
    graph, _ = init_metadata_analysis_pass(graph, pass_args=None)

    # create or load metadata.parameters and mase_graph.model
    if load_name is not None and load_type == "mz":
        graph, _ = load_mase_graph_interface_pass(graph, pass_args=load_name)
    else:
        dataset_info = get_dataset_info(dataset_name)
        model_info = models.get_model_info(model_name)
        data_module = MaseDataModule(
            model_name=model_name,
            name=dataset_name,
            batch_size=BATCH_SIZE,
            num_workers=0,
            tokenizer=None,
            max_token_len=None,
        )
        data_module.prepare_data()
        data_module.setup()
        dummy_in = get_dummy_input(
            model_info=model_info,
            data_module=data_module,
            task=task,
            device=accelerator,
        )  # generate dummy_in for generating metadata & generating weight mask
        if len(graph.model.additional_inputs) > 0:
            dummy_in = dummy_in | graph.model.additional_inputs
        graph, _ = add_common_metadata_analysis_pass(
            graph, pass_args={"dummy_in": dummy_in}
        )  # generate common_metadata
        graph, _ = add_software_metadata_analysis_pass(graph, pass_args=None)

    pass_config = config["passes"]  # config set in "toml" file
    huffman_pass_config = copy.deepcopy(pass_config)

    """
    Our pipeline consists of four main parts: Pruning, Quantization, Training, and Huffman Coding.
    Each part is implemented by an independent pass within transform, you can flexibly select and combine passes as needed.
    """
    for pass_name, pass_config in pass_config.items():
        pass_name: str
        pass_config: dict
        match pass_name:
            case "quantize":
                graph, _ = metadata_value_type_cast_transform_pass(
                    graph, pass_args={"fn": to_numpy_if_tensor}
                )
                pass_config["default"]["config"]["name"] = None
                pass_config["by"] = "type"
                graph, _ = PASSES["quantize"](graph, pass_args=pass_config)
                is_quantize = True

                """
                Weights will not be really quantized until we assign the quantized weights to them. 
                """
                for n in graph.nodes:
                    if isinstance(get_node_actual_target(n), torch.nn.modules.Conv2d):
                        if "mase" in n.meta:
                            if "resnet" in model_short_name:
                                quantized_weight = get_node_actual_target(
                                    n
                                ).w_quantizer(get_node_actual_target(n).weight)
                                parts = n.name.rsplit("_", 1)
                                parts[0] = parts[0].replace("_", ".")
                                modified_name = ".".join(parts) + ".weight"
                                graph.model.state_dict()[modified_name].copy_(
                                    quantized_weight
                                )
                                print(
                                    f"There is quantization at {n.name}, mase_op: {get_mase_op(n)}"
                                )
                            elif "vgg" in model_short_name:
                                quantized_weight = get_node_actual_target(
                                    n
                                ).w_quantizer(get_node_actual_target(n).weight)
                                graph.model.state_dict()[
                                    ".".join(n.name.rsplit("_", 1)) + ".weight"
                                ].copy_(quantized_weight)
                                print(
                                    f"There is quantization at {n.name}, mase_op: {get_mase_op(n)}"
                                )

                """
                model size after quantization:
                """
                model_size_after_quantize = model_storage_size(
                    graph.model, is_quantize, dict_weight_masks
                )
                print("model size after quantization: ", model_size_after_quantize)

                """
                save model after quantization (post-prune quantization)
                """
                save_dir = quantize_save_dir
                save_dir = Path(save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                graph, _ = metadata_value_type_cast_transform_pass(
                    graph, pass_args={"fn": to_numpy_if_tensor}
                )
                graph, _ = save_mase_graph_interface_pass(graph, pass_args=save_dir)
                logger.info(f"model is successfully quantized and saved to {save_dir}!")

            case "prune":
                input_generator = InputGenerator(
                    model_info=model_info,
                    data_module=data_module,
                    task=task,
                    which_dataloader="val",
                )
                print("pass_config")
                print(pass_config)  # print the config we set
                pass_config["model_name"] = model_name
                pass_config["input_generator"] = input_generator
                batch_size = config["passes"]["retrain"]["training"]["batch_size"]

                """
                number of Conv2d parameters before pruning
                """
                num_conv_param_before_prune = sum(
                    p.numel()
                    for m in model.modules()
                    if isinstance(m, torch.nn.Conv2d)
                    for p in m.parameters()
                )

                dict_weight_masks = (
                    {}
                )  # has the same value as weight_masks, but will be a form of dict
                """
                model size before pruning
                """
                model_size_before_prune = model_storage_size(
                    graph.model, is_quantize, dict_weight_masks
                )

                # pruning process
                graph, _ = PASSES[pass_name](
                    graph,
                    batch_size,
                    pass_config,
                )

                # calculate the pruning sparsity
                graph, sparsity_info, weight_masks, act_masks = PASSES[
                    "add_pruning_metadata"
                ](graph, {"dummy_in": dummy_in, "add_value": False})

                """
                weight pruning is of static process, where the weight mask for each layer remains during fine-tuning
                activation pruning is of dynamic pruning, where the activation mask for each input batch )iteration) will be updated

                if we want to force activation pruning to be static, we could run the following two lines to save activation masks
                """
                # torch.save(act_masks, "act_masks.pth")
                # print("activation mask saved")

                pp.pprint(sparsity_info)
                # del act_masks # to reduce memory

                """
                number of Conv2d parameters after pruning
                """
                num_conv_param_after_prune = 0
                for node in graph.fx_graph.nodes:
                    if node.op == "call_module":
                        if isinstance(graph.modules[node.target], torch.nn.Conv2d):
                            mask = (
                                graph.modules[node.target]
                                .parametrizations.weight[0]
                                .mask
                            )
                            dict_weight_masks[node.name] = mask
                            num_true = torch.sum(mask).item()
                            num_conv_param_after_prune += num_true

                """
                model size after pruning
                """
                model_size_after_prune = model_storage_size(
                    graph.model, is_quantize, dict_weight_masks
                )

                """
                number of FLOPs of Comv2d before and after pruning
                """
                conv_flop_before_prune, conv_flop_after_prune = conv_flop(
                    graph.model, act_masks, dict_weight_masks
                )

                """
                show 1)number of parameters  2)model size  3)number of FLOPs   before and after pruning:
                """
                print("-------------------------------------")
                print(
                    "number of Conv2d parameters before pruning: ",
                    num_conv_param_before_prune,
                )
                print("model size before pruning: ", model_size_before_prune)
                print("flop of Conv2d layers before pruning: ", conv_flop_before_prune)
                print("-------------------------------------")
                print(
                    "number of Conv2d parameters after pruning: ",
                    num_conv_param_after_prune,
                )
                print("model size after pruning: ", model_size_after_prune)
                print("flop of Conv2d layers after pruning: ", conv_flop_after_prune)
                print("-------------------------------------")
                print(
                    "reduced percentage of Conv2d parameters: ",
                    1 - num_conv_param_after_prune / num_conv_param_before_prune,
                )
                print(
                    "reduced percentage of model size: ",
                    1 - model_size_after_prune / model_size_before_prune,
                )
                print(
                    "reduced percentage of Conv2d flops: ",
                    1 - conv_flop_after_prune / conv_flop_before_prune,
                )
                print("-------------------------------------")

                """
                save the pruned model
                """
                save_dir = prune_save_dir
                save_dir = Path(save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                graph, _ = metadata_value_type_cast_transform_pass(
                    graph, pass_args={"fn": to_numpy_if_tensor}
                )
                graph, _ = save_mase_graph_interface_pass(graph, pass_args=save_dir)
                logger.info(f"model is successfully pruned and saved to {save_dir}!")

            case "retrain":
                """
                if loaded pre-trained model: fine-tuning
                if not: train from scratch
                """

                """
                compute the Hessian Matrix for gradient-based pruning (pruning at training)
                not used by default
                """
                from pytorch_lightning.callbacks import Callback

                class HessianComputationCallback(Callback):
                    def on_train_batch_end(
                        self, trainer, pl_module, outputs, batch, batch_idx
                    ):
                        loss = outputs["loss"]
                        named_parameters = list(pl_module.named_parameters())
                        name, param = named_parameters[1]
                        if "weight" in name:
                            hessian_diag = self.compute_hessian_diag(
                                param, pl_module, loss
                            )
                            print(
                                f"[Batch {batch_idx}] Hessian Diagonal for {name}: max={hessian_diag.max().item()}, min={hessian_diag.min().item()}, mean={hessian_diag.mean().item()}"
                            )

                    @staticmethod
                    def compute_hessian_diag(param, model, loss):
                        model.eval()
                        loss.requires_grad_(True)
                        first_order_grads = torch.autograd.grad(
                            loss, param, create_graph=True, allow_unused=True
                        )

                        hessian_diag = []
                        for grad in first_order_grads:
                            if grad is not None:
                                grad_grad = torch.autograd.grad(
                                    grad, param, retain_graph=True
                                )[0]
                                hessian_diag.append(grad_grad)

                        hessian_diag = torch.stack(hessian_diag).view_as(param)
                        return hessian_diag

                """
                use pytorch lightning model for training
                """
                plt_trainer_args = {}
                if retrain_save_dir is not None:
                    # if retrain_save_path is None, the model will not be saved
                    if not os.path.isdir(retrain_save_dir):
                        os.makedirs(retrain_save_dir)
                    checkpoint_callback = ModelCheckpoint(
                        save_top_k=1,
                        monitor="val_loss_epoch",
                        mode="min",
                        filename="best",
                        dirpath=retrain_save_dir,
                        save_last=True,
                    )
                    hessian_callback = HessianComputationCallback()
                    lr_monitor_callback = LearningRateMonitor(logging_interval="step")
                    plt_trainer_args["callbacks"] = [
                        checkpoint_callback,
                        # hessian_callback,
                        lr_monitor_callback,
                    ]

                plugins = None
                plt_trainer_args["plugins"] = plugins

                """
                build the model
                """
                wrapper_cls = get_model_wrapper(model_info, task)

                load_name = "../mase_output/group3_test/prune/state_dict.pt"
                load_type = "pt"

                if load_name:
                    """
                    load the weight masks generated by dummy-input, to do static weight pruning for each input batch
                    """
                    mask_collect = weight_masks
                    model = load_model(
                        model_short_name,
                        mask_collect,
                        is_quantize,
                        load_name,
                        load_type=load_type,
                        model=model,
                    )
                    logger.info(f"'{load_type}' checkpoint loaded before training")

                plt_trainer_args["accelerator"] = config["passes"]["retrain"][
                    "trainer"
                ]["accelerator"]
                plt_trainer_args["devices"] = config["passes"]["retrain"]["trainer"][
                    "devices"
                ]
                plt_trainer_args["limit_train_batches"] = 1
                plt_trainer_args["limit_val_batches"] = 0

                """
                basic hyperparameters
                """
                pl_model = wrapper_cls(
                    model,
                    dataset_info=dataset_info,
                    learning_rate=config["passes"]["retrain"]["training"][
                        "learning_rate"
                    ],
                    epochs=config["passes"]["retrain"]["training"]["max_epochs"],
                    weight_decay=config["passes"]["retrain"]["training"][
                        "weight_decay"
                    ],
                    optimizer=config["passes"]["retrain"]["training"]["optimizer"],
                    batch_size=config["passes"]["retrain"]["training"]["batch_size"],
                )

                """
                build trainer
                """
                trainer = pl.Trainer(
                    **plt_trainer_args,
                    max_epochs=config["passes"]["retrain"]["training"]["max_epochs"],
                )

                """
                train
                """
                trainer.fit(
                    pl_model,
                    datamodule=data_module,
                )

                save_dir = retrain_save_dir
                torch.save(pl_model.state_dict(), f"{save_dir}/model.ckpt")
                logger.info(
                    f"model is successfully fine-tuned and saved to {save_dir}/model.ckpt!"
                )

            case "huffman":
                """
                Huffman Encoding & Decoding are carried out as two separate passes
                """
                is_huffman = config["passes"]["huffman"]["is_huffman"]
                if is_huffman:
                    """
                    Huffman Encoding must follow quantization, so that weight elemnets are in a finite set
                    """
                    layer_huffman_info = PASSES["huffman"](
                        pl_model,
                        cf_args,
                        model_info,
                        data_module,
                        task,
                        accelerator,
                        huffman_pass_config,
                    )
                    """
                    Decode weights
                    """
                    decoded_weights = PASSES["huffman_decode"](layer_huffman_info)


run(config_file)
