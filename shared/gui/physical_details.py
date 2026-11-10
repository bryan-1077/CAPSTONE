"""Physical-design summaries from the selected backend attempt only."""
from .verification_details import small_json
from functools import lru_cache


@lru_cache(maxsize=8)
def _read_backend(path, modified, size):
    # Backend state includes repair histories and tool payloads. Retain a bounded
    # read, then cache only the fields presented by the GUI between polls.
    report = small_json(path, limit=16 * 1024 * 1024)
    fields = {'stage_status', 'last_error', 'timing_closure_status',
              'physical_verification_status', 'timing_closure_analysis', 'outputs',
              'layout_images', 'ssh_host', 'ssh_user', 'ssh_port', 'ssh_key_file'}
    for _, _, prefix in STEPS:
        fields.update(prefix + suffix for suffix in
                      ('_iteration', '_review_errors', '_review_warnings'))
    summary = {key: report[key] for key in fields if key in report}
    chosen = report.get('timing_closure_best_attempt')
    if isinstance(chosen, dict):
        summary['timing_closure_best_attempt'] = {
            key: chosen[key] for key in ('wns_ns', 'hold_wns_ns', 'limits', 'build_dir', 'outdir', 'report')
            if key in chosen}
    if not report:
        summary['_read_error'] = ('Backend report exceeds the 16 MiB viewer limit.' if size > 16 * 1024 * 1024
                                  else 'Backend report is empty, unreadable, or incomplete.')
    return summary

STEPS = [('rtl_prep', 'RTL Preparation', 'prep'), ('netlist', 'Synthesis', 'netlist'),
         ('mapped_netlist', 'Technology Mapping', 'mapped'), ('lec', 'Equivalence Checking', 'lec'),
         ('gdsii', 'Physical Implementation', 'gdsii')]


def backend_report(run):
    paths = sorted(run.directory.glob('[0-9]*_backend/backend_result.json'))
    path = paths[-1] if paths else None
    if path is None:
        return None, {}
    try:
        stat = path.stat()
        return path, _read_backend(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return path, {'_read_error': 'Backend report cannot be read.'}


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
        if report.get('_read_error'):
            rows.append(('Report availability', report['_read_error']))
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
        provenance = chosen.get('build_dir') or chosen.get('outdir') or chosen.get('report')
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
