"""Portable finding history with deterministic, explicit review transitions.

History is supplied by the selected report chain, never by searching ambient
review directories. Historical references are evidence to reassess, not proof
of a current defect or current approval.
"""
import copy

from architecture import fingerprint
from specification import SpecificationError, fields, names, text_value


def finding_key(manifest_fingerprint, finding):
    return 'L' + fingerprint({'manifest': manifest_fingerprint, 'finding': finding})[:24]


def _finding(value):
    fields(value, ('id', 'scope', 'resolution', 'message', 'references', 'requirement_ids'), (), 'ledger finding')
    text_value(value['id'], 'ledger finding id')
    text_value(value['message'], 'ledger finding message')
    if value['scope'] not in ('system', 'architecture') or value['resolution'] not in ('design_repair', 'external_agreement'):
        raise SpecificationError('Invalid ledger finding scope/resolution')
    if not names(value['references'], 'historical finding references'):
        raise SpecificationError('Historical findings require references')
    names(value['requirement_ids'], 'historical requirement IDs')


def _event(manifest_fingerprint, status, source, explanation, references, links):
    return {'manifest_fingerprint': manifest_fingerprint, 'status': status, 'source': source,
            'explanation': explanation, 'references': copy.deepcopy(references),
            'remaining_finding_ids': copy.deepcopy(links)}


def new_entries(manifest_fingerprint, findings):
    entries = []
    for finding in findings:
        _finding(finding)
        entries.append({'key': finding_key(manifest_fingerprint, finding),
            'introduced_manifest_fingerprint': manifest_fingerprint, 'finding': copy.deepcopy(finding),
            'status': 'open', 'history': [_event(manifest_fingerprint, 'open', 'finding',
                finding['message'], finding['references'], [finding['id']])]})
    return entries


def validate_entries(entries):
    if not isinstance(entries, list):
        raise SpecificationError('Ledger entries must be a list')
    keys = []
    for entry in entries:
        fields(entry, ('key', 'introduced_manifest_fingerprint', 'finding', 'status', 'history'), (), 'ledger entry')
        _finding(entry['finding'])
        text_value(entry['introduced_manifest_fingerprint'], 'introduced manifest')
        if entry['key'] != finding_key(entry['introduced_manifest_fingerprint'], entry['finding']):
            raise SpecificationError('Ledger finding identity changed')
        keys.append(entry['key'])
        history = entry['history']
        initial = new_entries(entry['introduced_manifest_fingerprint'], [entry['finding']])[0]['history'][0]
        if not isinstance(history, list) or not history or history[0] != initial:
            raise SpecificationError('Ledger must retain its original finding event')
        for event in history:
            fields(event, ('manifest_fingerprint', 'status', 'source', 'explanation', 'references',
                           'remaining_finding_ids'), (), 'ledger event')
            text_value(event['manifest_fingerprint'], 'event manifest')
            text_value(event['explanation'], 'event explanation')
            if event['status'] not in ('open', 'resolved') or event['source'] not in (
                    'finding', 'repair_assessment', 'finding_assessment'):
                raise SpecificationError('Invalid ledger event')
            if not names(event['references'], 'ledger event references'):
                raise SpecificationError('Ledger disposition needs evidence')
            links = names(event['remaining_finding_ids'], 'ledger event links')
            if bool(links) != (event['status'] == 'open'):
                raise SpecificationError('Open ledger events need finding links; resolved events must not')
        if entry['status'] != history[-1]['status']:
            raise SpecificationError('Ledger status differs from its recorded disposition')
    names(keys, 'ledger entry keys')


def validate_sources(sources, manifest_fingerprint, repair_context=None):
    if not isinstance(sources, list):
        raise SpecificationError('Finding history must be a list of report sources')
    seen = {}
    for source in sources:
        fields(source, ('report_fingerprint', 'manifest_fingerprint', 'entries'), (), 'finding history source')
        for key in ('report_fingerprint', 'manifest_fingerprint'):
            text_value(source[key], key)
        same = source['manifest_fingerprint'] == manifest_fingerprint
        parent = repair_context is not None and (
            source['manifest_fingerprint'] == repair_context['parent_manifest_fingerprint']
            and source['report_fingerprint'] == repair_context['parent_report_fingerprint'])
        if not same and not parent:
            raise SpecificationError('Finding history belongs to another artifact/configuration or repair parent')
        validate_entries(source['entries'])
        previous = seen.setdefault(source['report_fingerprint'], source)
        if previous != source:
            raise SpecificationError('Conflicting history for the same report')
    merge_entries(sources)


def merge_entries(sources):
    merged = {}
    for source in sources:
        for entry in source['entries']:
            old = merged.get(entry['key'])
            if old is not None:
                if any(old[k] != entry[k] for k in ('introduced_manifest_fingerprint', 'finding')):
                    raise SpecificationError('Ledger identity collision')
                a, b = old['history'], entry['history']
                if a[:min(len(a), len(b))] != b[:min(len(a), len(b))]:
                    raise SpecificationError('Divergent ledger histories require an explicit review chain')
                if len(a) >= len(b):
                    continue
            merged[entry['key']] = copy.deepcopy(entry)
    return [merged[key] for key in sorted(merged)]


def source_from_report(report):
    """Caller validates the report against its artifact before selecting it."""
    ledger = report.get('finding_ledger')
    entries = ledger['entries'] if ledger is not None else new_entries(
        report['manifest_fingerprint'], report['verdict']['findings'])
    return {'report_fingerprint': fingerprint(report), 'manifest_fingerprint': report['manifest_fingerprint'],
            'entries': copy.deepcopy(entries)}


def sources_for_review(repair_context=None, extra=()):
    sources = copy.deepcopy(repair_context.get('finding_history', []) if repair_context else [])
    if not isinstance(sources, list):
        raise SpecificationError('Finding history must be a list of report sources')
    sources.extend(copy.deepcopy(extra))
    result = {}
    for source in sources:
        fields(source, ('report_fingerprint', 'manifest_fingerprint', 'entries'), (), 'finding history source')
        text_value(source['report_fingerprint'], 'report fingerprint')
        key = source['report_fingerprint']
        if key in result and result[key] != source:
            raise SpecificationError('Conflicting history for the same report')
        result[key] = source
    return list(result.values())


def obligations(sources, repair_context=None):
    originals = repair_context['findings'] if repair_context else []
    return [entry for entry in merge_entries(sources) if entry['status'] == 'open'
            and entry['finding'] not in originals]


def validate_assessments(verdict, sources, repair_context, snapshot, resolve):
    required = obligations(sources, repair_context)
    if not required:
        return
    assessments = verdict['finding_assessments']
    if not isinstance(assessments, list):
        raise SpecificationError('finding_assessments must be a list')
    by_key = {entry['key']: entry for entry in required}
    seen = []
    for assessment in assessments:
        fields(assessment, ('ledger_key', 'status', 'explanation', 'references', 'remaining_finding_ids'), (), 'finding assessment')
        text_value(assessment['ledger_key'], 'ledger_key')
        seen.append(assessment['ledger_key'])
        if assessment['ledger_key'] not in by_key or assessment['status'] not in ('resolved', 'unresolved'):
            raise SpecificationError('Assess a known ledger finding as resolved or unresolved')
        text_value(assessment['explanation'], 'finding assessment explanation')
        if not names(assessment['references'], 'finding assessment references'):
            raise SpecificationError('Finding assessment needs current evidence')
        for ref in assessment['references']:
            resolve(snapshot, ref)
        links = names(assessment['remaining_finding_ids'], 'finding assessment links')
        current = {f['id'] for f in verdict['findings'] if
                   f['resolution'] == by_key[assessment['ledger_key']]['finding']['resolution']}
        if bool(links) != (assessment['status'] == 'unresolved') or set(links) - current:
            raise SpecificationError('Unresolved ledger findings must link current findings of the same resolution kind')
    names(seen, 'assessed ledger keys')
    if set(seen) != set(by_key):
        raise SpecificationError('Assess every outstanding ledger finding exactly once')


def build_ledger(manifest_fingerprint, verdict, sources, repair_context=None, *, settled=True):
    entries = merge_entries(sources)
    originals = {f['id']: f for f in repair_context['findings']} if repair_context else {}
    repair = {a['finding_id']: a for a in verdict.get('repair_assessments', [])}
    carried = {a['ledger_key']: a for a in verdict.get('finding_assessments', [])}
    for entry in entries:
        if entry['status'] != 'open' or not settled:
            continue
        if entry['finding'] in originals.values():
            assessment, source = repair[entry['finding']['id']], 'repair_assessment'
        else:
            assessment, source = carried[entry['key']], 'finding_assessment'
        status = 'resolved' if assessment['status'] == 'resolved' else 'open'
        entry['history'].append(_event(manifest_fingerprint, status, source, assessment['explanation'],
                                       assessment['references'], assessment['remaining_finding_ids']))
        entry['status'] = status
    by_key = {e['key']: e for e in entries}
    for entry in new_entries(manifest_fingerprint, verdict['findings']):
        old = by_key.get(entry['key'])
        if old is None:
            by_key[entry['key']] = entry
        elif old['status'] == 'resolved':
            old['history'].extend(entry['history'])
            old['status'] = 'open'
    return {'version': 1, 'sources': copy.deepcopy(sources),
            'entries': [by_key[key] for key in sorted(by_key)]}


def validate_ledger(report, manifest_fingerprint):
    if 'finding_ledger' not in report:  # Historical approvals/reports remain readable.
        return
    ledger = report['finding_ledger']
    fields(ledger, ('version', 'sources', 'entries'), (), 'finding ledger')
    if type(ledger['version']) is not int or ledger['version'] != 1:
        raise SpecificationError('Unsupported finding ledger version')
    validate_sources(ledger['sources'], manifest_fingerprint, report.get('repair_context'))
    selected = {s['report_fingerprint']: s for s in ledger['sources']}
    for source in sources_for_review(report.get('repair_context')):
        if selected.get(source['report_fingerprint']) != source:
            raise SpecificationError('Finding ledger omitted its repair-parent history')
    validate_entries(ledger['entries'])
    if report['status'] == 'failed' and report['verdict'] is None:
        if ledger['entries'] != merge_entries(ledger['sources']):
            raise SpecificationError('Failed transport/parse review changed its input ledger')
        return
    if ledger != build_ledger(manifest_fingerprint, report['verdict'], ledger['sources'],
                              report.get('repair_context'), settled=report['status'] != 'failed'):
        raise SpecificationError('Finding ledger dropped or changed recorded findings/dispositions')
    if report['status'] == 'passed' and any(e['status'] == 'open' for e in ledger['entries']):
        raise SpecificationError('Outstanding ledger findings prevent approval')
