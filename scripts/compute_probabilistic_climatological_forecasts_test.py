# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from absl.testing import absltest
from absl.testing import flagsaver
from absl.testing import parameterized
import numpy as np
import pandas as pd
from weatherbench2 import schema
from weatherbench2 import utils
import xarray as xr
import xarray_beam

from . import compute_probabilistic_climatological_forecasts as cpcf


class GetSampledInitTimesTest(parameterized.TestCase):
  """Test this private method, mostly because the style guide says not to."""

  def assert_uniform(self, values, stderr_tol):
    """Asserts values counts uniformly distributed up to 4 standard errors."""
    counts = pd.Series(values).value_counts().sort_index()
    ensemble_size = counts.sum()
    fracs = counts / ensemble_size
    expected_frac = 1 / len(counts)
    standard_error = np.sqrt(
        expected_frac * (1 - expected_frac) / ensemble_size
    )
    np.testing.assert_allclose(
        fracs, expected_frac, atol=stderr_tol * standard_error
    )

  @parameterized.named_parameters(
      dict(
          testcase_name='WithReplacement_Ensemble50',
          with_replacement=True,
          ensemble_size=50,
      ),
      dict(
          testcase_name='WithoutReplacement_Ensemble50',
          with_replacement=False,
          ensemble_size=50,
      ),
      dict(
          testcase_name='WithReplacement_EnsembleNeg1',
          with_replacement=True,
          ensemble_size=-1,
      ),
      dict(
          testcase_name='WithoutReplacement_EnsembleNeg1',
          with_replacement=False,
          ensemble_size=-1,
      ),
  )
  def test_sample_statistics(self, with_replacement, ensemble_size):
    output_times = pd.to_datetime(
        ['1990-01-02T12', '1995-06-01T00', '2000-12-30T07']
    )
    day_window_size = 6
    climatology_start_year = 1990
    climatology_end_year = 2001
    sampled_times = cpcf._get_sampled_init_times(
        output_times,
        climatology_start_year=climatology_start_year,
        climatology_end_year=climatology_end_year,
        day_window_size=day_window_size,
        ensemble_size=ensemble_size,
        with_replacement=with_replacement,
        seed=802701,
    )
    allowed_sample_years = cpcf._get_possible_year_values(
        climatology_start_year, climatology_end_year
    )

    expected_ensemble_size = cpcf._get_ensemble_size(
        ensemble_size,
        climatology_start_year,
        climatology_end_year,
        day_window_size,
    )

    for output_idx in range(len(output_times)):
      output_t = output_times[output_idx]

      # Get the implied (integer day) perturbation used in sampling.
      # centers are the dates we would sample if we day_window_size = 0
      allowed_centers = (
          (allowed_sample_years - 1970).astype('datetime64[Y]')
          + np.array(output_t.dayofyear - 1, dtype='timedelta64[D]')
          + np.array(output_t.hour, dtype='timedelta64[h]')
      )
      sampled_t = sampled_times[:, output_idx]
      self.assertLen(sampled_t, expected_ensemble_size)
      center = allowed_centers[
          np.argmin(
              np.abs(allowed_centers[:, np.newaxis] - sampled_t[np.newaxis]),
              axis=0,
          )
      ]
      perturbation = pd.to_timedelta(sampled_t - center)

      # We should perturb by an integer number of days.
      np.testing.assert_array_equal(0, perturbation.seconds)

      # The selected day perturbation should be uniformly distributed.
      # ...but, they will not be perfectly uniform at the edges since we turn
      # e.g. Jan1 - 3 days into Dec31 - 2 days.
      no_edge_effects = (
          day_window_size < output_t.dayofyear < 365 - day_window_size
      )
      expect_everything_sampled_once = (
          not with_replacement and ensemble_size == -1
      )
      if no_edge_effects:
        self.assert_uniform(
            perturbation.days,
            stderr_tol=0 if expect_everything_sampled_once else 4,
        )
        self.assertEqual(perturbation.days.min(), -day_window_size // 2)
        self.assertEqual(
            perturbation.days.max(),
            day_window_size // 2 + day_window_size % 2 - 1,
        )

      # The years should be uniform.
      years = pd.to_datetime(sampled_t).year
      self.assertEqual(years.min(), climatology_start_year)
      self.assertEqual(years.max(), climatology_end_year)
      self.assert_uniform(
          years, stderr_tol=0 if expect_everything_sampled_once else 4
      )


class MainTest(parameterized.TestCase):

  def _make_dataset_that_grows_by_one_with_every_timedelta(
      self,
      input_time_resolution: str,
      timedelta_spacing: str,
  ):
    ds = utils.random_like(
        schema.mock_truth_data(
            variables_2d=[],
            variables_3d=['temperature', 'geopotential'],
            time_start='2000-01-01',
            time_stop='2005-01-01',
            spatial_resolution_in_degrees=90.0,
            time_resolution=input_time_resolution,
        )
    )
    # This ds grows by 1 every input_time_resolution step.
    ds = ds.isel(time=0).expand_dims(time=ds.time) + xr.DataArray(
        data=np.arange(len(ds.time)), dims=('time',), coords=dict(time=ds.time)
    )
    # Now, ds grows by 1 every timedelta step.
    ds *= pd.Timedelta(input_time_resolution) / pd.Timedelta(timedelta_spacing)
    return ds

  @parameterized.named_parameters(
      dict(testcase_name='Default'),
      dict(testcase_name='CustomTimeName', time_dim='init'),
      dict(testcase_name='OddWindow', day_window_size=3),
      dict(testcase_name='OutputIsLeapYearInFeb', output_leap_location='feb'),
      dict(testcase_name='OutputIsLeapYearInDec', output_leap_location='dec'),
      dict(testcase_name='DataHasLeapYear', data_year_hasleap=True),
      dict(
          testcase_name='DeltaLessThanInit',
          initial_time_spacing='2d',
          timedelta_spacing='1d',
          custom_prediction_timedelta_chunk=True,
          with_replacement=False,
          ensemble_size=-1,
      ),
      dict(
          testcase_name='DeltaNotEqualToInitBothLong',
          initial_time_spacing='4d',
          timedelta_spacing='2d',
      ),
      dict(
          testcase_name='SubDayTimedeltaAndInitBothSame',
          initial_time_spacing='12h',
          timedelta_spacing='12h',
          input_time_resolution='12h',
      ),
      dict(
          testcase_name='SubDayTimedeltaLessThanInit',
          initial_time_spacing='12h',
          timedelta_spacing='6h',
          input_time_resolution='6h',
      ),
      dict(
          testcase_name='SubDayTimedeltaGreaterThanInit',
          initial_time_spacing='6h',
          timedelta_spacing='12h',
          input_time_resolution='6h',
      ),
      dict(
          testcase_name='DeltaGreaterThanInit',
          initial_time_spacing='1d',
          timedelta_spacing='2d',
          custom_prediction_timedelta_chunk=True,
          with_replacement=False,
          ensemble_size=-1,
      ),
  )
  def test_standard_workflow(
      self,
      initial_time_spacing='1d',
      timedelta_spacing='1d',
      input_time_resolution='1d',
      day_window_size=4,
      time_dim='time',
      output_leap_location=None,
      data_year_hasleap=False,
      custom_prediction_timedelta_chunk=False,
      with_replacement=True,
      ensemble_size=20,
  ):
    input_ds = self._make_dataset_that_grows_by_one_with_every_timedelta(
        input_time_resolution=input_time_resolution,
        timedelta_spacing=timedelta_spacing,
    )
    if time_dim != 'time':
      input_ds = input_ds.rename({'time': time_dim})

    input_path = self.create_tempdir('source').full_path
    output_path = self.create_tempdir('destination').full_path

    input_chunks = {time_dim: 3, 'longitude': 6, 'latitude': 5, 'level': 3}
    input_ds.chunk(input_chunks).to_zarr(input_path)

    forecast_duration = '3d'

    if output_leap_location == 'feb':
      initial_time_start = '2004-02-25'
      initial_time_end = '2004-03-01'
    elif output_leap_location == 'dec':
      initial_time_start = '2004-12-25'
      initial_time_end = '2005-01-05'
    else:
      assert output_leap_location is None, output_leap_location
      initial_time_start = '2004-01-01'
      initial_time_end = '2004-01-15'
    output_dates_have_leap = output_leap_location is not None

    if data_year_hasleap:
      climatology_start_year = 2000
      climatology_end_year = 2002
    else:
      climatology_start_year = 2001
      climatology_end_year = 2003

    expected_ensemble_size = cpcf._get_ensemble_size(
        ensemble_size,
        climatology_start_year,
        climatology_end_year,
        day_window_size,
    )

    output_chunks_flag = f'{time_dim}=1,level=1' + (
        ',prediction_timedelta=2' if custom_prediction_timedelta_chunk else ''
    )

    with flagsaver.as_parsed(
        input_path=input_path,
        output_path=output_path,
        climatology_start_year=str(climatology_start_year),
        climatology_end_year=str(climatology_end_year),
        time_dim=time_dim,
        initial_time_start=initial_time_start,
        initial_time_end=initial_time_end,
        initial_time_spacing=initial_time_spacing,
        forecast_duration=forecast_duration,
        timedelta_spacing=timedelta_spacing,
        day_window_size=str(day_window_size),
        ensemble_size=str(ensemble_size),
        with_replacement=str(with_replacement).lower(),
        variables='temperature',
        output_chunks=output_chunks_flag,
        runner='DirectRunner',
    ):
      cpcf.main([])

    output_ds, output_chunks = xarray_beam.open_zarr(output_path)

    # Check chunks and dataset sizes.
    expected_output_chunks = {
        time_dim: 1,
        'longitude': min(
            input_chunks['longitude'], output_ds.sizes['longitude']
        ),
        'latitude': min(input_chunks['latitude'], output_ds.sizes['latitude']),
        'realization': expected_ensemble_size,
        'prediction_timedelta': output_ds.sizes['prediction_timedelta'],
        'level': 1,  # level was explicitly specified
    }
    if custom_prediction_timedelta_chunk:
      expected_output_chunks |= {'prediction_timedelta': 2}
    self.assertEqual(expected_output_chunks, output_chunks)

    for dim in ['latitude', 'longitude', 'level']:
      self.assertEqual(input_ds.sizes[dim], output_ds.sizes[dim], msg=f'{dim=}')
    self.assertEqual(expected_ensemble_size, output_ds.sizes['realization'])

    # Check dimension values
    pd.testing.assert_index_equal(
        pd.date_range(
            initial_time_start, initial_time_end, freq=initial_time_spacing
        ),
        pd.to_datetime(output_ds[time_dim].data),
    )
    pd.testing.assert_index_equal(
        pd.timedelta_range('0h', forecast_duration, freq=timedelta_spacing),
        pd.to_timedelta(output_ds.prediction_timedelta.data),
    )

    # Check variables (this is the exciting part!)
    self.assertCountEqual(['temperature'], list(output_ds))

    # Ensemble members differ.
    np.testing.assert_array_less(0, output_ds.temperature.var('realization'))

    # Test the correct timedeltas were scattered to the right init times.
    # Recall we ensured values were increasing by 1 every timedelta
    np.testing.assert_allclose(
        1, output_ds.temperature.diff('prediction_timedelta')
    )

    timedeltas_in_a_year = pd.Timedelta(
        f'{365 + output_dates_have_leap}d'
    ) / pd.Timedelta(timedelta_spacing)
    timedeltas_in_a_day = pd.Timedelta('1d') / pd.Timedelta(timedelta_spacing)

    # Check that the initial times output_t, came from input days of year within
    # the specified window. Use the fact that temperature is growing at a rate
    # of 1 for every timedelta.
    for region in [
        dict(latitude=0, longitude=0, level=0),
        dict(
            latitude=-1,
            longitude=-1,
            level=-1,
        ),
    ]:
      for i_time in [-1, -2]:
        msg = f'{i_time=}, {region=}'
        temperature = (
            output_ds.isel(region).isel({time_dim: i_time}).temperature
        )
        # Since temperature is growing linearly at a rate of 1 for every
        # timedelta, we expect a certain spread of temperatures...roughly equal
        # to the day_window_size. There are edge effects due to way windows
        # extending outside of one year (and being "modded" back to the
        # beginning of the same year).
        #
        # The last init time should not have edge effects, so the sampled times
        # for output_t should come from
        # times in [output_t - day_window_size/2, output_t + day_window_size/2]
        dist_from_minval = (temperature % timedeltas_in_a_year) - (
            temperature % timedeltas_in_a_year
        ).min('realization')
        dist = np.minimum(
            np.abs(dist_from_minval),
            365
            + bool(output_leap_location == 'dec')
            - np.abs(dist_from_minval),
        )
        if initial_time_spacing == timedelta_spacing and not (
            output_dates_have_leap or data_year_hasleap
        ):
          # In this case, there are sampled times at exactly +-day_window_size/2
          # so we expect a perfect match.
          np.testing.assert_allclose(
              dist.max() / timedeltas_in_a_day,
              # The spread is the center + the window sides.
              day_window_size - 1,
              err_msg=msg,
          )
        else:
          # The sampled times may not match up with the window.
          self.assertLessEqual(
              int(dist.max()) / timedeltas_in_a_day,
              day_window_size
              # This test checks the implied day of year of the sample. Since
              # every leap year means a shift in day of year, we add a buffer
              # here to allow for an imprecise match.
              + data_year_hasleap + output_dates_have_leap,
              msg=msg,
          )


if __name__ == '__main__':
  absltest.main()
