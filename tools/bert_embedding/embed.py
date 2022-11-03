# coding=utf-8
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from functools import partial
import h5py
import numpy as np
import os
import time
import torch
from torch.utils.data import BatchSampler, DataLoader, SequentialSampler, Subset
from torch.utils.data._utils.collate import default_collate

from megatron import get_args, get_tokenizer, mpu, print_rank_0
from megatron.model import BertModel, ModelType
from megatron.schedules import get_forward_backward_func
from megatron.training import setup_model_and_optimizer

from .dataset import BertEmbeddingDataset
from .utils import get_missing_blocks_by_rank

# >>>
from lutil import pax, print_seq
from lutil.pax import print_mem_stats, get_mem_stats_str
# <<<


def model_provider(pre_process=True, post_process=True):
    """Build the model."""

    print_rank_0(" > build Bert model.")

    args = get_args()
    num_tokentypes = 2 if args.bert_binary_head else 0
    model = BertModel(
        num_tokentypes=num_tokentypes,
        add_binary_head=args.bert_binary_head,
        parallel_output=True,
        pre_process=pre_process,
        post_process=post_process)

    return model


def get_batch(data_iterator):
    """Build the batch."""

    # Items and their type.
    keys = ['text', 'types', 'labels', 'is_random', 'loss_mask', 'padding_mask',
            'seq_length']
    datatype = torch.int64

    # Broadcast data.
    if data_iterator is not None:
        data = next(data_iterator)
    else:
        data = None
    data_b = mpu.broadcast_data(keys, data, datatype)

    # Unpack.
    tokens = data_b['text'].long()
    types = data_b['types'].long()
    sentence_order = data_b['is_random'].long()
    loss_mask = data_b['loss_mask'].float()
    lm_labels = data_b['labels'].long()
    padding_mask = data_b['padding_mask'].long()
    seq_lengths = data_b['seq_length'].long()

    return tokens, types, sentence_order, loss_mask, lm_labels, padding_mask, \
        seq_lengths


def loss_func(loss_mask, sentence_order, seq_lengths,
              output_tensor, non_loss_data):
    """Loss function. Sequence lengths returned here for progress print-outs."""
    assert non_loss_data
    return seq_lengths, output_tensor


def forward_step(data_iterator, model):
    """Forward step."""

    args = get_args()

    # Get the batch.
    tokens, types, sentence_order, loss_mask, lm_labels, padding_mask, \
        seq_lengths = get_batch(data_iterator)

    if not args.bert_binary_head:
        types = None

    # Forward pass through the model.
    output_tensor = model(tokens, padding_mask, tokentype_ids=types,
                          lm_labels=lm_labels)

    return output_tensor, partial(loss_func, loss_mask, sentence_order,
                                  seq_lengths)


def collate_batch(samples):
    """Collate samples of various lengths.

    This collate function handles samples with various sequence lengths, by
    padding 'text' arrays with pad_id, and other arrays with 0.
    """

    n_samples = len(samples)
    keys = list(samples[0].keys())
    tokenizer = get_tokenizer()

    # Max sample length across all samples.
    max_length_map = { key:0 for key in keys }
    for sample in samples:
        for key in keys:
            value_length = \
                len(sample[key]) if isinstance(sample[key], np.ndarray) else None
            max_length_map[key] = None \
                if value_length is None else \
                   max(max_length_map[key], value_length)

    # Pad samples.
    padded_samples = []
    for sample in samples:
        padded_sample = {}
        for key in keys:
            padded_sample[key] = \
                np.pad(
                    sample[key],
                    (0, max_length_map[key] - len(sample[key])),
                    mode = "constant",
                    constant_values = tokenizer.pad_id if key == "text" else 0,
                ) \
                if isinstance(sample[key], np.ndarray) else \
                   sample[key]
        padded_samples.append(padded_sample)

    # Build batch with padded samples.
    batch = default_collate(padded_samples)

    return batch


# def get_block_data_loader(full_dataset, sample_start_idx, sample_end_idx):
# def get_subset_data_loader(full_dataset, sample_start_idx, sample_end_idx):
def get_data_loader(dataset):
    """Build data loader over data subset.

    Get a subset of the dataset (from start_idx -> end_idx), and wrap it in
    a sequential sampler and data loader.
    """

    args = get_args()

    # Sequential & batch samplers.
    batch_sampler = BatchSampler(
        sampler = SequentialSampler(dataset),
        batch_size = args.micro_batch_size,
        drop_last = False,
    )

    # Data loader.
    data_loader = DataLoader(dataset,
                             batch_sampler = batch_sampler,
                             num_workers = args.num_workers,
                             pin_memory = True,
                             collate_fn = collate_batch)

    return data_loader


# def embed_batches(models, data_loader):
def embed_data_loader(models, data_loader, n_samples_world):

    # Data iterator.
    data_iterator = iter(data_loader)

    # Eval mode.
    for m in models:
        m.eval()

    # World info (for printing progress).
    n_gpus_world = torch.distributed.get_world_size()
    # n_samples_world = len(data_loader.dataset.dataset) # i.e., subset.dataset

    # Compute embeddings.
    forward_backward_func = get_forward_backward_func()
    with torch.no_grad():

        # Iterate batches.
        n_batches = len(data_iterator)
        dataset_start_time = time.time()
        batch_times = []
        max_seq_lengths = []
        embeddings = []
        for batch_index in range(n_batches):

            # Forward pass.
            batch_start_time = time.time()
            results = forward_backward_func(
                forward_step,
                data_iterator,
                models,
                optimizer = None,
                timers = None,
                forward_only = True,
                collect_non_loss_data = True,
            )
            batch_end_time = time.time()
            batch_times.append(batch_end_time - batch_start_time)
            mean_batch_time = sum(batch_times[-8:]) / min(len(batch_times), 8)

            # Collect embeddings.
            assert len(results) == 1, "assert len(models) == 1 before this"
            seq_lengths, output_tensor = results[0]
            max_seq_lengths.append(seq_lengths.max().item())
            embeddings.append(output_tensor.cpu().numpy())

            # Progress.
            if batch_index % 50 == 0:
                est_dataset_time = (batch_end_time - dataset_start_time) + \
                    (n_batches - batch_index - 1) * mean_batch_time
                samples_per_sec = len(data_loader.dataset) / est_dataset_time
                print_rank_0("batch %d / %d [%d] ... sq %.1f, %.3f samples/sec [ full dataset w/ %d gpu(s): %.3f hours ] ... %s." % (
                    batch_index,
                    n_batches,
                    max_seq_lengths[-1],
                    sum(max_seq_lengths) / len(max_seq_lengths),
                    samples_per_sec,
                    n_gpus_world,
                    (n_samples_world / samples_per_sec) / n_gpus_world / 3600,
                    get_mem_stats_str(),
                ))

    return np.concatenate(embeddings, axis = 0)


class BertEmbedder:

    def __init__(self, max_bert_seq_length):
        self.models, optimizer, opt_param_scheduler = \
            setup_model_and_optimizer(model_provider,
                                      ModelType.encoder_or_decoder)
        self.max_bert_seq_length = max_bert_seq_length


    def embed_text_dataset(self, text_dataset, n_samples_world):
        bert_dataset = BertEmbeddingDataset(text_dataset,
                                            self.max_bert_seq_length)
        data_loader = get_data_loader(bert_dataset)
        # pax(0, {
        #     "text_dataset" : text_dataset,
        #     "bert_dataset" : bert_dataset,
        #     "data_loader" : data_loader,
        # })
        embeddings = embed_data_loader(self.models, data_loader, n_samples_world)
        # pax(0, {"embeddings": embeddings})
        return embeddings


# class DiskDataParallelBertEmbedder(BertEmbedder):
class DiskDataParallelBertEmbedder:

    def __init__(self, max_bert_seq_length, block_size):
        # super().__init__(max_bert_seq_length)
        self.embedder = BertEmbedder(max_bert_seq_length)
        self.block_size = block_size


    def embed_text_blocks(self, name, workdir, text_dataset,
                          missing_embedding_blocks):

        # Iterate blocks.
        for block_index, block_info in enumerate(missing_embedding_blocks):

            # Missing block lists are extended with None to have equal-length
            # lists. Skip the Nones.
            if block_info is not None:

                print_rank_0("embed '%s' block %d / %d ... %s." % (
                    name,
                    block_index,
                    len(missing_embedding_blocks),
                    block_info["path"],
                ))

                # Embed block.
                sub_dataset = Subset(text_dataset, range(*block_info["range"]))
                embeddings = self.embedder.embed_text_dataset(sub_dataset,
                                                              len(text_dataset))

                # Save embeddings.
                f = h5py.File(block_info["path"], "w")
                f.create_dataset("data", data = embeddings)
                f.close()

            # Synchronize progress across all ranks. (for easier observation)
            print_rank_0(" > waiting for other ranks to finish block.")
            torch.distributed.barrier()


    def embed_text_dataset(self, name, workdir, text_dataset):

        # Dataset workdir.
        os.makedirs(workdir, exist_ok = True)

        # Missing embedding blocks (stored on disk).
        def validate(f):
            assert f["data"].shape[1] == 1024
        n_missing_world, missing_embedding_blocks = get_missing_blocks_by_rank(
            workdir,
            len(text_dataset),
            self.block_size,
            validate = validate)

        # Prevent missing file race condition.
        torch.distributed.barrier()

        # Embed batches.
        self.embed_text_blocks(name, workdir, text_dataset,
                               missing_embedding_blocks)


    def embed_text_datasets(self, text_dataset_map):

        # Embed each (i.e., full, sampled) dataset.
        for name, info in text_dataset_map.items():
            print_rank_0(" > embed '%s' dataset ... %d samples." %
                         (name, len(info["data"])))
            self.embed_text_dataset_on_disk(name, info["embed_dir"], info["data"])
