from __future__ import annotations

# """Sorting components: clustering"""
from pathlib import Path

import shutil
import numpy as np

try:
    import hdbscan

    HAVE_HDBSCAN = True
except:
    HAVE_HDBSCAN = False

import random, string, os
from spikeinterface.core.basesorting import minimum_spike_dtype
from spikeinterface.core import get_global_tmp_folder, get_channel_distances, get_random_data_chunks
from sklearn.preprocessing import QuantileTransformer, MaxAbsScaler
from spikeinterface.core.waveform_tools import extract_waveforms_to_buffers, estimate_templates
from .clustering_tools import remove_duplicates, remove_duplicates_via_matching, remove_duplicates_via_dip
from spikeinterface.core import NumpySorting
from spikeinterface.core import extract_waveforms
from spikeinterface.core.recording_tools import get_noise_levels
from spikeinterface.core.job_tools import fix_job_kwargs
from spikeinterface.sortingcomponents.waveforms.savgol_denoiser import SavGolDenoiser
from spikeinterface.sortingcomponents.features_from_peaks import RandomProjectionsFeature
from spikeinterface.core.template import Templates
from spikeinterface.core.sparsity import compute_sparsity
from spikeinterface.sortingcomponents.tools import remove_empty_templates
from spikeinterface.core.node_pipeline import (
    run_node_pipeline,
    ExtractDenseWaveforms,
    ExtractSparseWaveforms,
    PeakRetriever,
)


class RandomProjectionClustering:
    """
    hdbscan clustering on peak_locations previously done by localize_peaks()
    """

    _default_params = {
        "hdbscan_kwargs": {
            "min_cluster_size": 20,
            "allow_single_cluster": True,
            "core_dist_n_jobs": -1,
            "cluster_selection_method": "leaf",
        },
        "cleaning_kwargs": {},
        "waveforms": {"ms_before": 2, "ms_after": 2},
        "sparsity": {"method": "ptp", "threshold": 0.25},
        "radius_um": 50,
        "nb_projections": 10,
        "ms_before": 0.5,
        "ms_after": 0.5,
        "random_seed": 42,
        "noise_levels": None,
        "smoothing_kwargs": {"window_length_ms": 0.25},
        "tmp_folder": None,
        "job_kwargs": {},
    }

    @classmethod
    def main_function(cls, recording, peaks, params):
        assert HAVE_HDBSCAN, "random projections clustering need hdbscan to be installed"

        job_kwargs = fix_job_kwargs(params["job_kwargs"])

        d = params
        if "verbose" in job_kwargs:
            verbose = job_kwargs["verbose"]
        else:
            verbose = False

        fs = recording.get_sampling_frequency()
        nbefore = int(params["ms_before"] * fs / 1000.0)
        nafter = int(params["ms_after"] * fs / 1000.0)
        num_samples = nbefore + nafter
        num_chans = recording.get_num_channels()
        np.random.seed(d["random_seed"])

        if params["tmp_folder"] is None:
            name = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            tmp_folder = get_global_tmp_folder() / name
        else:
            tmp_folder = Path(params["tmp_folder"]).absolute()

        tmp_folder.mkdir(parents=True, exist_ok=True)

        node0 = PeakRetriever(recording, peaks)
        node1 = ExtractSparseWaveforms(
            recording,
            parents=[node0],
            return_output=False,
            ms_before=params["ms_before"],
            ms_after=params["ms_after"],
            radius_um=params["radius_um"],
        )

        node2 = SavGolDenoiser(recording, parents=[node0, node1], return_output=False, **params["smoothing_kwargs"])

        num_projections = min(num_chans, d["nb_projections"])
        projections = np.random.randn(num_chans, num_projections)
        if num_chans > 1:
            projections -= projections.mean(0)
            projections /= projections.std(0)

        nbefore = int(params["ms_before"] * fs / 1000)
        nafter = int(params["ms_after"] * fs / 1000)
        nsamples = nbefore + nafter

        node3 = RandomProjectionsFeature(
            recording,
            parents=[node0, node2],
            return_output=True,
            projections=projections,
            radius_um=params["radius_um"],
            sparse=True,
        )

        pipeline_nodes = [node0, node1, node2, node3]

        hdbscan_data = run_node_pipeline(
            recording, pipeline_nodes, job_kwargs=job_kwargs, job_name="extracting features"
        )

        import sklearn

        clustering = hdbscan.hdbscan(hdbscan_data, **d["hdbscan_kwargs"])
        peak_labels = clustering[0]

        labels = np.unique(peak_labels)
        labels = labels[labels >= 0]

        spikes = np.zeros(np.sum(peak_labels > -1), dtype=minimum_spike_dtype)
        mask = peak_labels > -1
        spikes["sample_index"] = peaks[mask]["sample_index"]
        spikes["segment_index"] = peaks[mask]["segment_index"]
        spikes["unit_index"] = peak_labels[mask]

        unit_ids = np.arange(len(np.unique(spikes["unit_index"])))

        nbefore = int(params["waveforms"]["ms_before"] * fs / 1000.0)
        nafter = int(params["waveforms"]["ms_after"] * fs / 1000.0)

        templates_array = estimate_templates(
            recording, spikes, unit_ids, nbefore, nafter, return_scaled=False, job_name=None, **job_kwargs
        )

        templates = Templates(
            templates_array, fs, nbefore, None, recording.channel_ids, unit_ids, recording.get_probe()
        )
        if params["noise_levels"] is None:
            params["noise_levels"] = get_noise_levels(recording, return_scaled=False)
        sparsity = compute_sparsity(templates, params["noise_levels"], **params["sparsity"])
        templates = templates.to_sparse(sparsity)
        templates = remove_empty_templates(templates)

        if verbose:
            print("We found %d raw clusters, starting to clean with matching..." % (len(templates.unit_ids)))

        cleaning_matching_params = job_kwargs.copy()
        for value in ["chunk_size", "chunk_memory", "total_memory", "chunk_duration"]:
            if value in cleaning_matching_params:
                cleaning_matching_params[value] = None
        cleaning_matching_params["chunk_duration"] = "100ms"
        cleaning_matching_params["n_jobs"] = 1
        cleaning_matching_params["verbose"] = False
        cleaning_matching_params["progress_bar"] = False

        cleaning_params = params["cleaning_kwargs"].copy()
        cleaning_params["tmp_folder"] = tmp_folder

        labels, peak_labels = remove_duplicates_via_matching(
            templates, peak_labels, job_kwargs=cleaning_matching_params, **cleaning_params
        )

        if verbose:
            print("We kept %d non-duplicated clusters..." % len(labels))

        return labels, peak_labels
