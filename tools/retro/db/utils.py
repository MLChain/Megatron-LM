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

import glob
import h5py
import json
import numpy as np
import os

from megatron import get_retro_args, print_rank_0
from megatron.data.indexed_dataset import make_dataset as make_indexed_dataset

from .dataset import GPTChunkDataset

# >>>
from lutil import pax
# <<<


def get_base_db_workdir():
    args = get_retro_args()
    return os.path.join(args.retro_workdir, "db")


def get_indexed_dataset_infos_path():
    return os.path.join(get_base_db_workdir(), "indexed_dataset_infos.json")


def save_indexed_dataset_infos(indexed_dataset_infos):
    """Save dataset order."""

    # Remove 'dataset' field.
    clean_infos = []
    for info in indexed_dataset_infos:
        info = dict(info)
        del info["dataset"]
        clean_infos.append(info)

    # Save.
    with open(get_indexed_dataset_infos_path(), "w") as f:
        json.dump(clean_infos, f, indent = 4)


def get_indexed_dataset_infos():

    # Load json.
    path = get_indexed_dataset_infos_path()
    with open(path) as f:
        infos = json.load(f)

    # Add indexed datasets.
    for info in infos:
        info["dataset"] = make_indexed_dataset(info["prefix"], "mmap", True)

    return infos


def get_individual_db_info(name):
    return {
        "db_dir" : os.path.join(get_base_db_workdir(), "individual", name, "db"),
    }


def get_individual_db(ds_id, ds_info):
    db_paths = sorted(glob.glob(ds_info["db_dir"] + "/*hdf5"))
    db = np.zeros((ds_info["n_chunks_valid"], 5), dtype = "i8")
    db[:, 0] = ds_id
    start_idx = 0
    for db_path in db_paths:
        f = h5py.File(db_path, "r")
        n_chunks_current = f["chunks_valid"].shape[0]
        db[start_idx:(start_idx+n_chunks_current), 1:] = f["chunks_valid"]
        start_idx += n_chunks_current
        f.close()

    assert start_idx == ds_info["n_chunks_valid"]

    return db


def get_merged_db_path_map():
    base_dir = get_base_db_workdir()
    return {
        # "full" : os.path.join(base_dir, "merged", "full.hdf5"),
        "train" : os.path.join(base_dir, "merged", "train.hdf5"),
        "sampled" : os.path.join(base_dir, "merged", "sampled.hdf5"),
    }


def get_merged_dataset(db_type, indexed_dataset_infos = None):

    args = get_retro_args()

    if not indexed_dataset_infos:
        indexed_dataset_infos = get_indexed_dataset_infos()

    # Load chunk db.
    db_path = get_merged_db_path_map()[db_type]
    f = h5py.File(db_path, "r")
    chunk_db = np.copy(f["chunks"])
    f.close()

    # Chunk dataset.
    indexed_datasets = [ info["dataset"] for info in indexed_dataset_infos ]
    chunk_dataset = GPTChunkDataset(indexed_datasets, chunk_db,
                                    args.retro_gpt_chunk_length)

    return chunk_dataset


# def get_full_merged_dataset(indexed_dataset_infos = None, force = False):
#     if not force:
#         raise Exception("only load 'n_chunks_train' chunks.")
#     return get_merged_dataset("full", indexed_dataset_infos)
# def get_merged_training_dataset(indexed_dataset_infos = None):
def get_merged_train_dataset(indexed_dataset_infos = None):
    return get_merged_dataset("train", indexed_dataset_infos)


# def get_sampled_merged_dataset(indexed_dataset_infos = None):
def get_merged_sampled_dataset(indexed_dataset_infos = None):
    return get_merged_dataset("sampled", indexed_dataset_infos)


# def create_data_softlinks(data_files):

#     # Soft links. [ personal space ]
#     root_dir = \
#         "/gpfs/fs1/projects/gpu_adlr/datasets/lmcafee/retro/preprocess/data"
#     for data_index, global_file in enumerate(data_files):

#         print("soft links, data %d / %d." % (data_index, len(data_files)))

#         local_dir = os.path.join(
#             root_dir,
#             os.path.basename(os.path.dirname(global_file)),
#         )
#         local_prefix = os.path.join(
#             local_dir,
#             os.path.splitext(os.path.basename(global_file))[0],
#         )
#         global_prefix = os.path.splitext(global_file)[0]

#         if not os.path.exists(local_dir):
#             os.mkdir(local_dir)

#         for ext in [ "bin", "idx" ]:
#             local_file = local_prefix + "." + ext
#             if not os.path.exists(local_file):
#                 os.symlink(global_prefix + "." + ext, local_file)

#         # pax(0, {
#         #     "global_file" : global_file,
#         #     "root_dir" : root_dir,
#         #     "local_dir" : local_dir,
#         #     "local_prefix" : local_prefix,
#         #     "global_prefix" : global_prefix,
#         # })

#     pax(0, {"data_files": data_files})
#     # raise Exception("soft link.")
