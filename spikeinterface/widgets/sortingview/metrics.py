import numpy as np

from ...core.core_tools import check_json
from ..base import to_attr
from .base_sortingview import SortingviewPlotter, generate_unit_table_view


class MetricsPlotter(SortingviewPlotter):

    def do_plot(self, data_plot, **backend_kwargs):
        import sortingview.views as vv

        dp = to_attr(data_plot)
        backend_kwargs = self.update_backend_kwargs(**backend_kwargs)

        metrics = dp.metrics

        if dp.unit_ids is None:
            unit_ids = metrics.index.values
        else:
            unit_ids = dp.uniit_ids
        unit_ids = self.make_serializable(unit_ids)

        metrics_sv = []
        for col in metrics.columns:
            dtype = metrics.iloc[0][col].dtype
            metric = vv.UnitMetricsGraphMetric(
                            key=col,
                            label=col,
                            dtype=dtype.str
                        )
            metrics_sv.append(metric)

        units_m = []
        for unit_id in unit_ids:
            values = check_json(metrics.loc[unit_id].to_dict())
            values_skip_nans = {}
            for k, v in values.items():
                if np.isnan(v):
                    continue
                values_skip_nans[k] = v
            
            units_m.append(
                vv.UnitMetricsGraphUnit(
                    unit_id=unit_id,
                    values=values_skip_nans
                )
            )
        v_metrics = vv.UnitMetricsGraph(
                units=units_m,
                metrics=metrics_sv
            )

        if not dp.hide_unit_selector:
            v_units_table = generate_unit_table_view(dp.sorting)

            view = vv.Box(
                direction="horizontal",
                items=[
                    vv.LayoutItem(v_units_table, max_size=150),
                    vv.LayoutItem(v_metrics),
                ],
            )
        else:
            view = v_metrics

        self.handle_display_and_url(view, **backend_kwargs)
        return view
