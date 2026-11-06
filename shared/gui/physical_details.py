"""Physical-design summaries from the selected backend attempt only."""
from .verification_details import small_json

STEPS = [('rtl_prep', 'RTL Preparation', 'prep'), ('netlist', 'Synthesis', 'netlist'),
         ('mapped_netlist', 'Technology Mapping', 'mapped'), ('lec', 'Equivalence Checking', 'lec'),
         ('gdsii', 'Physical Implementation', 'gdsii')]


def backend_report(run):
    paths = sorted(run.directory.glob('[0-9]*_backend/backend_result.json'))
    path = paths[-1] if paths else None
    return path, small_json(path) if path else {}


def physical_steps(run, *, alive=False):
    path, report = backend_report(run)
    statuses = report.get('stage_status', {})
    statuses = statuses if isinstance(statuses, dict) else {}
    attempted = any(r.get('stage') == 'backend' for r in run.results) or path is not None
    result = []
    for key, title, prefix in STEPS:
        saved = statuses.get(key)
        skipped = run.config.get('no_backend') or (key == 'lec' and run.config.get('skip_lec'))
        if saved in (None, 'pending') and skipped:
            status = 'Skipped'
        elif saved is None:
            status = 'Evidence unavailable' if attempted else 'Not reached'
        elif saved == 'pending':
            status = 'Not reached'
        else:
            status = {'success': 'Passed', 'running': 'In progress' if alive else 'Unconfirmed'}.get(
                saved, str(saved).replace('_', ' ').capitalize())
        rows = []
        iteration = report.get(prefix + '_iteration')
        if isinstance(iteration, int) and iteration > 0:
            rows.append(('Review iterations', str(iteration)))
        for name, label in [('review_errors', 'Review errors'), ('review_warnings', 'Warnings')]:
            values = report.get(prefix + '_' + name, [])
            if isinstance(values, list):
                rows.extend((label, str(value)) for value in values[:20])
        if not rows:
            rows.append(('Evidence', 'No step result recorded.' if saved in (None, 'pending') else
                         'See the backend report and stage logs for the recorded checks.'))
        if path:
            rows.append(('Report', str(path)))
        result.append((key, title, status, rows))
    return result


def measured_results(report):
    """Keep values attached to the chosen build; never substitute configured limits."""
    rows = []
    chosen = report.get('timing_closure_best_attempt')
    if isinstance(chosen, dict) and chosen:
        for key, title, unit in [('wns_ns', 'Setup WNS', 'ns'), ('hold_wns_ns', 'Hold WNS', 'ns')]:
            value = chosen.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append((title, f'{value:g} {unit}'))
        limits = chosen.get('limits', {})
        if isinstance(limits, dict):
            for key, title, unit in [('die_area_mm2', 'Die Area', 'mm²'), ('total_power_w', 'Reported Power', 'W')]:
                value = limits.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    rows.append((title, f'{value:g} {unit}'))
        provenance = chosen.get('build_dir') or chosen.get('report')
        if provenance:
            rows.append(('Measured Build', str(provenance)))
    analysis = report.get('timing_closure_analysis', {})
    if not rows and isinstance(analysis, dict):
        for key, title in [('wns', 'Reported WNS'), ('tns', 'Reported TNS')]:
            value = analysis.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append((title, f'{value:g} ns'))
    for key, title in [('timing_closure_status', 'Timing Closure'), ('physical_verification_status', 'Physical Checks')]:
        if report.get(key):
            rows.append((title, str(report[key]).replace('_', ' ').capitalize()))
    return rows
