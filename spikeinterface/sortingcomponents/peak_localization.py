"""Sorting components: peak localization."""

import numpy as np

from spikeinterface.core.job_tools import ChunkRecordingExecutor, _shared_job_kwargs_doc
from spikeinterface.toolkit import get_channel_distances

from ..toolkit import get_chunk_with_margin

from ..toolkit.postprocessing.unit_localization import (dtype_localize_by_method,
                                                        possible_localization_methods,
                                                        solve_monopolar_triangulation,
                                                        make_radial_order_parents,
                                                        enforce_decrease_shells_ptp)


def init_kwargs_dict(method, method_kwargs, recording):
    """Initialize a dictionary of keyword arguments."""

    if method == 'center_of_mass':
        method_kwargs_ = dict(local_radius_um=150)
    elif method == 'monopolar_triangulation':
        method_kwargs_ = dict(
            local_radius_um=150,
            max_distance_um=1000,
            optimizer='minimize_with_log_penality',
            enforce_decrease_radial_parents=None,
        )

    method_kwargs_.update(method_kwargs)

    if method_kwargs_.get("enforce_decrease", None) is not None:
        contact_locations = recording.get_channel_locations()
        channel_distance = get_channel_distances(recording)
        neighbours_mask = channel_distance < method_kwargs.get('local_radius_um', 150)
        method_kwargs_["enforce_decrease_radial_parents"] = make_radial_order_parents(
            contact_locations, neighbours_mask
        )

    return method_kwargs_


def localize_peaks(recording, peaks, ms_before=1, ms_after=1, method='center_of_mass',
                   method_kwargs={}, **job_kwargs):
    """Localize peak (spike) in 2D or 3D depending the method.

    When a probe is 2D then:
       * X is axis 0 of the probe
       * Y is axis 1 of the probe
       * Z is orthogonal to the plane of the probe

    Parameters
    ----------
    recording: RecordingExtractor
        The recording extractor object.
    peaks: array
        Peaks array, as returned by detect_peaks() in "compact_numpy" way.
    ms_before: float
        The left window, before a peak, in milliseconds.
    ms_after: float
        The right window, after a peak, in milliseconds.
    method: 'center_of_mass' or 'monopolar_triangulation'
        Method to use.
    method_kwargs: dict of kwargs method
        Keyword arguments for the chosen method:
            'center_of_mass':
                * local_radius_um: float
                    For channel sparsity.
            'monopolar_triangulation':
                * local_radius_um: float
                    For channel sparsity.
                * max_distance_um: float, default: 1000
                    Boundary for distance estimation.
                * enforce_decrese : None or "radial"
                    If+how to enforce spatial decreasingness for PTP vectors.
    {}

    Returns
    -------
    peak_locations: ndarray
        Array with estimated location for each spike.
        The dtype depends on the method. ('x', 'y') or ('x', 'y', 'z', 'alpha').
    """
    assert method in possible_localization_methods, f"Method {method} is not supported. Choose from {possible_localization_methods}"

    # handle default method_kwargs
    contact_locations = recording.get_channel_locations()
    method_kwargs = init_kwargs_dict(method, method_kwargs, recording)

    nbefore = int(ms_before * recording.get_sampling_frequency() / 1000.)
    nafter = int(ms_after * recording.get_sampling_frequency() / 1000.)

    # margin at border for get_trace
    margin = max(nbefore, nafter)

    # TODO
    # make a memmap for peaks to avoid serialization

    # and run
    func = _localize_peaks_chunk
    init_func = _init_worker_localize_peaks
    init_args = (recording.to_dict(), peaks, method, method_kwargs, nbefore, nafter, contact_locations, margin)
    processor = ChunkRecordingExecutor(recording, func, init_func, init_args, handle_returns=True,
                                       job_name='localize peaks', **job_kwargs)
    peak_locations = processor.run()

    peak_locations = np.concatenate(peak_locations)

    return peak_locations


localize_peaks.__doc__ = localize_peaks.__doc__.format(_shared_job_kwargs_doc)


def _init_worker_localize_peaks(recording, peaks, method, method_kwargs,
                                nbefore, nafter, contact_locations, margin):
    """Initialize worker for localizing peaks."""

    if isinstance(recording, dict):
        from spikeinterface.core import load_extractor
        recording = load_extractor(recording)

    # create a local dict per worker
    worker_ctx = {}
    worker_ctx['recording'] = recording
    worker_ctx['peaks'] = peaks
    worker_ctx['method'] = method
    worker_ctx['method_kwargs'] = method_kwargs
    worker_ctx['nbefore'] = nbefore
    worker_ctx['nafter'] = nafter

    worker_ctx['contact_locations'] = contact_locations
    worker_ctx['margin'] = margin

    if method in ('center_of_mass', 'monopolar_triangulation'):
        # handle sparsity
        channel_distance = get_channel_distances(recording)
        neighbours_mask = channel_distance < method_kwargs['local_radius_um']
        worker_ctx['neighbours_mask'] = neighbours_mask

    return worker_ctx


def _localize_peaks_chunk(segment_index, start_frame, end_frame, worker_ctx):
    """Localize peaks in a chunk of data."""

    # recover variables of the worker
    recording = worker_ctx['recording']
    peaks = worker_ctx['peaks']
    method = worker_ctx['method']
    nbefore = worker_ctx['nbefore']
    nafter = worker_ctx['nafter']
    neighbours_mask = worker_ctx['neighbours_mask']
    contact_locations = worker_ctx['contact_locations']
    margin = worker_ctx['margin']

    # load trace in memory
    # traces = recording.get_traces(start_frame=start_frame, end_frame=end_frame,
    #                               segment_index=segment_index)
    recording_segment = recording._recording_segments[segment_index]
    traces, left_margin, right_margin = get_chunk_with_margin(recording_segment, start_frame, end_frame,
                                                              None, margin, add_zeros=True)

    # get local peaks (sgment + start_frame/end_frame)
    i0 = np.searchsorted(peaks['segment_ind'], segment_index)
    i1 = np.searchsorted(peaks['segment_ind'], segment_index + 1)
    peak_in_segment = peaks[i0:i1]
    i0 = np.searchsorted(peak_in_segment['sample_ind'], start_frame)
    i1 = np.searchsorted(peak_in_segment['sample_ind'], end_frame)
    local_peaks = peak_in_segment[i0:i1]

    # make sample index local to traces
    local_peaks = local_peaks.copy()
    local_peaks['sample_ind'] -= (start_frame - left_margin)

    if method == 'center_of_mass':
        peak_locations = localize_peaks_center_of_mass(traces, local_peaks, contact_locations,
                                                       neighbours_mask, nbefore, nafter)
    elif method == 'monopolar_triangulation':
        max_distance_um = worker_ctx['method_kwargs']['max_distance_um']
        optimizer = worker_ctx['method_kwargs']['optimizer']
        enforce_decrease_radial_parents = worker_ctx['method_kwargs']['enforce_decrease_radial_parents']
        peak_locations = localize_peaks_monopolar_triangulation(
            traces, local_peaks, contact_locations,
            neighbours_mask, nbefore, nafter, max_distance_um, optimizer,
            enforce_decrease_radial_parents=enforce_decrease_radial_parents,
        )

    return peak_locations


def localize_peaks_center_of_mass(traces, local_peak, contact_locations, neighbours_mask,
                                  nbefore, nafter):
    """Localize peaks using the center of mass method."""

    peak_locations = np.zeros(local_peak.size, dtype=dtype_localize_by_method['center_of_mass'])

    for i, peak in enumerate(local_peak):
        chan_mask = neighbours_mask[peak['channel_ind'], :]
        chan_inds, = np.nonzero(chan_mask)

        local_contact_locations = contact_locations[chan_inds, :]

        wf = traces[peak['sample_ind'] - nbefore:peak['sample_ind'] + nafter, :][:, chan_inds]
        wf_ptp = wf.ptp(axis=0)
        com = np.sum(wf_ptp[:, np.newaxis] * local_contact_locations, axis=0) / np.sum(wf_ptp)

        peak_locations['x'][i] = com[0]
        peak_locations['y'][i] = com[1]

    return peak_locations


def localize_peaks_monopolar_triangulation(traces, local_peak, contact_locations, neighbours_mask,
                                           nbefore, nafter, max_distance_um, optimizer,
                                           enforce_decrease_radial_parents=None):
    """Localize peaks using the monopolar triangulation method.

    Notes
    -----
    This method is from  Julien Boussard, Erdem Varol and Charlie Windolf
    See spikeinterface.toolkit.postprocessing.unit_localization.
    """
    peak_locations = np.zeros(local_peak.size, dtype=dtype_localize_by_method['monopolar_triangulation'])

    for i, peak in enumerate(local_peak):
        sample_ind = peak['sample_ind']
        chan_mask = neighbours_mask[peak['channel_ind'], :]
        chan_inds = np.flatnonzero(chan_mask)
        local_contact_locations = contact_locations[chan_inds, :]

        # wf is (nsample, nchan) - chan is only neighbor
        wf = traces[sample_ind - nbefore:sample_ind + nafter, chan_inds]
        wf_ptp = wf.ptp(axis=0)
        if enforce_decrease_radial_parents is not None:
            enforce_decrease_shells_ptp(
                wf_ptp, peak['channel_ind'], enforce_decrease_radial_parents, in_place=True
            )
        peak_locations[i] = solve_monopolar_triangulation(wf_ptp, local_contact_locations, max_distance_um, optimizer)

    return peak_locations
