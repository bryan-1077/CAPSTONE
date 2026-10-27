"""Repair review obligations and change evidence; no extra model calls."""
import copy

from architecture import fingerprint
from specification import SpecificationError, fields, names, text_value
from specification_change_audit import audit_changes


def build_repair_context(parent, repaired, plan):
    def evidence(manifest):
        snapshot = copy.deepcopy(manifest['provenance']['specification']['snapshot'])
        # Audit history is not current defect evidence. Compiler identity changes
        # also do not describe behavior; the full artifacts remain fingerprinted.
        snapshot['package'].pop('generation', None)
        return snapshot
    result = {'version': 1, 'parent_manifest_fingerprint': fingerprint(parent),
            'parent_report_fingerprint': plan['report_fingerprint'],
            'target_manifest_fingerprint': fingerprint(repaired),
            'findings': copy.deepcopy(plan['findings']),
            'changes': audit_changes(evidence(parent), evidence(repaired))}
    if 'finding_history' in plan:
        result['finding_history'] = copy.deepcopy(plan['finding_history'])
    return result


def validate_repair_context(context, manifest):
    fields(context, ('version', 'parent_manifest_fingerprint', 'parent_report_fingerprint',
                     'target_manifest_fingerprint', 'findings', 'changes'), ('finding_history',), 'repair context')
    if type(context['version']) is not int or context['version'] != 1:
        raise SpecificationError('Unsupported repair context version')
    for key in ('parent_manifest_fingerprint', 'parent_report_fingerprint', 'target_manifest_fingerprint'):
        text_value(context[key], key)
    if context['target_manifest_fingerprint'] != fingerprint(manifest):
        raise SpecificationError('Repair context belongs to another target revision')
    if not isinstance(context['findings'], list) or not context['findings']:
        raise SpecificationError('Repair context needs original design findings')
    ids = []
    for finding in context['findings']:
        fields(finding, ('id', 'scope', 'resolution', 'message', 'references', 'requirement_ids'), (), 'original finding')
        text_value(finding['id'], 'original finding id')
        if finding['resolution'] != 'design_repair' or finding['scope'] not in ('system', 'architecture'):
            raise SpecificationError('Repair obligations must be original design findings')
        ids.append(finding['id'])
    names(ids, 'original finding ids')
    if not isinstance(context['changes'], list):
        raise SpecificationError('Repair changes must be a list')
    if 'finding_history' in context:
        from finding_ledger import validate_sources
        validate_sources(context['finding_history'], fingerprint(manifest), context)


def validate_repair_assessments(verdict, context, snapshot, resolve):
    """Require every original issue to be assessed and any open issue linked."""
    expected = {f['id'] for f in context['findings']}
    current = {f['id'] for f in verdict['findings'] if f['resolution'] == 'design_repair'}
    assessments = verdict['repair_assessments']
    if not isinstance(assessments, list):
        raise SpecificationError('repair_assessments must be a list')

    def evidence(value):
        text_value(value['explanation'], 'assessment explanation')
        if not names(value['references'], 'assessment references'):
            raise SpecificationError('Assessment requires current evidence references')
        for ref in value['references']:
            resolve(snapshot, ref)

    def linked(value, key, needs_links):
        links = names(value[key], key)
        if bool(links) != needs_links or set(links) - current:
            raise SpecificationError('Open issues/regressions must link current design findings; resolved checks must not')

    seen = []
    for assessment in assessments:
        fields(assessment, ('finding_id', 'status', 'explanation', 'references', 'remaining_finding_ids'), (), 'repair assessment')
        text_value(assessment['finding_id'], 'assessment finding_id')
        seen.append(assessment['finding_id'])
        if assessment['status'] not in ('resolved', 'unresolved'):
            raise SpecificationError('Repair assessment status must be resolved or unresolved')
        evidence(assessment)
        linked(assessment, 'remaining_finding_ids', assessment['status'] == 'unresolved')
    names(seen, 'assessed finding ids')
    if set(seen) != expected:
        raise SpecificationError('Assess every original design finding exactly once')
    preservation = fields(verdict['preservation_assessment'],
        ('status', 'explanation', 'references', 'regression_finding_ids'), (), 'preservation assessment')
    if preservation['status'] not in ('preserved', 'regressions_found'):
        raise SpecificationError('Preservation status must be preserved or regressions_found')
    evidence(preservation)
    linked(preservation, 'regression_finding_ids', preservation['status'] == 'regressions_found')
