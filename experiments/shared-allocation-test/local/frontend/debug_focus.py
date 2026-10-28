"""Small assessment working sets with explicit competing explanations."""
import re
import hashlib

SOURCE_BUDGET = 22000
EXCERPT_BUDGET = 2400


def clipped(value, limit=800):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + ' [abridged; request source if needed]'


def failure_overview(summary):
    if not isinstance(summary,dict):
        return clipped(summary,4000)
    result = dict(summary)
    for key in ('flow_errors','testbench_errors','assert_failures','verilator_diagnostics'):
        if key not in summary:
            continue
        result[key] = []
        for error in summary[key][:8]:
            result[key].append({field:clipped(value,1200) if isinstance(value,str) else value
                for field,value in error.items()
                if field in ('file','line','log_line','message','severity','code','source','stage','module')
                or (field=='raw' and not error.get('message'))})
    return result


def excerpt(item, terms, requested=None):
    lines = item['text'].splitlines()
    # Keep small RTL/checker blocks intact. Cutting a 3 KB queue module into
    # snippets can omit the very sequential assignment the model needs.
    if len(item['text']) <= EXCERPT_BUDGET or (
            item.get('kind') in ('rtl','checker') and len(item['text']) <= 6000):
        return item['text'], False
    first = item.get('start_line', 1)
    if requested and requested.get('end_line',first+600) - requested.get('start_line',first) <= 100:
        start = max(0, requested.get('start_line', first) - first)
        end = min(len(lines), requested.get('end_line', first + len(lines) - 1) - first + 1)
        candidates = list(range(start, end))
    else:
        ranked = []
        for i, line in enumerate(lines):
            lower = line.lower()
            score = sum(len(term) for term in terms if term in lower)
            if re.search(r'<=|(?<![=!<>])=(?!=)', line):
                score += 10
            if re.search(r'error|fatal|mismatch|assert|expected|observed', lower):
                score += 20
            ranked.append((score, -i))
        centers = []
        for _, neg in sorted(ranked, reverse=True):
            if all(abs(-neg - previous) > 12 for previous in centers):
                centers.append(-neg)
            if len(centers) == 3:
                break
        candidates = list(dict.fromkeys(i for center in centers
                                       for i in range(max(0, center - 5), min(len(lines), center + 7))))
    selected, used = [], 0
    for i in candidates:
        if used + len(lines[i]) + 1 > EXCERPT_BUDGET:
            break
        selected.append(i)
        used += len(lines[i]) + 1
    rendered, previous = [], -2
    for i in sorted(selected):
        if i != previous + 1:
            rendered.append('[unselected lines omitted; available by focused read]')
        rendered.append(lines[i])
        previous = i
    return '\n'.join(rendered), True


def prompt_context(record):
    """Archive is unchanged; only selected excerpts are resent to the model."""
    hypotheses = record.get('hypotheses', [])
    focus = record.get('focus', {})
    counter = set(focus.get('counterevidence', []))
    support = set()
    for hypothesis in hypotheses:
        counter.update(hypothesis.get('contradictory_evidence', []))
        support.update(hypothesis.get('supporting_evidence', []))
    recent_actions = record['actions'][record.get('last_batch_start', max(0,len(record['actions'])-3)):]
    recent = {ref for action in recent_actions for ref in action.get('evidence_ids', [])}
    requests = {action['request'].get('path'): action['request'] for action in recent_actions
                if action['request'].get('tool') == 'read'}
    words = ' '.join([focus.get('failure', ''), focus.get('alternative', ''),
                      *[h.get('cause', '') for h in hypotheses[:2]]])
    terms = [word.lower() for word in re.findall(r'[A-Za-z_][A-Za-z_0-9]{3,}',words)]
    def recalled(item):
        request = requests.get(item['path'])
        return bool(request and request.get('start_line',1) <= item.get('end_line',1)
                    and request.get('end_line',600) >= item.get('start_line',1))
    candidates = sorted(record['evidence'], key=lambda e: (
        e['id'] not in counter, not recalled(e), e['id'] not in recent,
        e['id'] not in support, e.get('kind') not in ('observation','checker'),
        -int(e['id'][1:])))
    # Ensure the working set includes primary RTL, original observations/checker,
    # and counterevidence rather than spending it all on one source kind.
    required = [e for e in candidates if e['id'] in counter]
    for kind in ('rtl','observation','checker'):
        match = next((e for e in candidates if e.get('kind') == kind),None)
        if match and match not in required:
            required.append(match)
    ordered = required + [e for e in candidates if e not in required]
    presented, used = [], 0
    for item in ordered:
        text, partial = excerpt(item,terms,requests.get(item['path']))
        if not text:
            continue
        if used + len(text) > SOURCE_BUDGET:
            continue
        presented.append({key:item[key] for key in ('id','path','kind','start_line','end_line','sha256') if key in item})
        presented[-1].update(text=text, partial=partial)
        used += len(text)
    visible = {e['id'] for e in presented}
    presented_lines = {}
    for item in presented:
        presented_lines.setdefault(item['path'],set()).update(
            int(match.group(1)) for match in re.finditer(r'^(\d+):',item['text'],re.M))
    record['presented_lines'] = {name:sorted(lines) for name,lines in presented_lines.items()}
    record['presented_evidence_ids'] = sorted(visible)
    record['prompt_source_chars'] = used
    record['working_context_signature'] = hashlib.sha256(
        '\n'.join(e['id']+e['text'] for e in presented).encode()).hexdigest()
    important_paths = {e['path'] for e in presented}
    important_paths.update(action.get('path') for action in record['pending'])
    important_paths.update(record.get('recent_catalog', []))
    catalog = record['catalog']
    names = sorted(catalog, key=lambda name: (name not in important_paths,
        catalog[name] not in ('rtl','checker'), '/work/compile/' in name, name))[:40]
    history = []
    for action in record['actions'][-6:]:
        request = action['request']
        history.append(dict(tool=request.get('tool'), path=request.get('path'),
            reason=clipped(request.get('reason',''),250), status=action['status'],
            evidence_ids=action.get('evidence_ids', []), error=clipped(action.get('error',''),300)))
    return dict(evidence=presented, evidence_archive=[
        dict(id=e['id'],path=e['path'],kind=e.get('kind'),start_line=e.get('start_line'),
             end_line=e.get('end_line'),presented=e['id'] in visible) for e in record['evidence']],
        catalog={name:catalog[name] for name in names},
        catalog_total=len(catalog), actions=history,
        source_characters=used, source_limit=SOURCE_BUDGET,
        focus=focus, focus_history=record.get('focus_history', [])[-4:],
        reconsideration_required=record.get('reconsideration_required',False))


def accept_focus(record, data, decision):
    """Require a falsifiable alternative; refuted causes cannot silently return."""
    hypotheses = data.get('hypotheses', [])
    if not record.get('focused_mode') or not hypotheses:
        return
    presented = record.get('presented_evidence_ids')
    if presented is not None:
        for hypothesis in hypotheses:
            for key in ('supporting_evidence','contradictory_evidence'):
                if set(hypothesis[key]) - set(presented):
                    raise ValueError('Recall archived evidence with a focused read before citing it')
    focus = data.get('focus')
    if not isinstance(focus,dict):
        raise ValueError('Provide focus: failure, hypothesis_id, alternative, discriminating_check, predictions and check_result')
    for field in ('failure','hypothesis_id','alternative','discriminating_check',
                  'primary_prediction','alternative_prediction'):
        if not isinstance(focus.get(field),str) or not focus[field].strip():
            raise ValueError(f'focus requires {field}')
        if len(focus[field]) > 1600:
            raise ValueError(f'focus {field} is too long')
    if focus['alternative'].strip().lower() == hypotheses[0]['cause'].strip().lower():
        raise ValueError('Alternative must describe a competing explanation')
    if focus['primary_prediction'].strip().lower() == focus['alternative_prediction'].strip().lower():
        raise ValueError('Discriminating check needs different predictions for the two explanations')
    result = focus.get('check_result')
    if result not in ('not_run','supports_primary','supports_alternative','inconclusive'):
        raise ValueError('Invalid focus check_result')
    refs = focus.get('result_evidence', [])
    counters = focus.get('counterevidence', [])
    known = {e['id'] for e in record['evidence']}
    for values in (refs,counters):
        if not isinstance(values,list) or any(ref not in known for ref in values):
            raise ValueError('Focus evidence must cite saved evidence IDs')
        if presented is not None and set(values) - set(presented):
            raise ValueError('Recall archived evidence before using it as a check result or counterevidence')
    if result != 'not_run' and not refs:
        raise ValueError('Report a check outcome only with result_evidence citations')
    old = record.get('focus', {})
    identity = focus['hypothesis_id']
    if old and identity != old.get('hypothesis_id') and not focus.get('revision_reason'):
        raise ValueError('Explain a changed primary hypothesis with revision_reason')
    retired = record.get('refuted_hypotheses', {})
    if identity in retired:
        prior = set(retired[identity])
        if not focus.get('revision_reason') or not set(refs) - prior:
            raise ValueError('Previously refuted hypothesis needs new result evidence and revision_reason')
    if record.get('focus_review_required') and not focus.get('revision_reason'):
        raise ValueError('Address failed repair feedback with revision_reason; distinguish partial repair from a wrong diagnosis')
    if decision == 'sufficient_evidence' and result != 'supports_primary':
        raise ValueError('Patch readiness requires a discriminating check supporting the primary hypothesis')
    unchanged = identity == old.get('hypothesis_id')
    old_ids = set(record.get('focus_evidence_seen', []))
    new_ids = known - old_ids
    new_view = record.get('working_context_signature') != record.get('focus_seen_signature')
    stagnant = record.get('focus_stagnation',0) + 1 if unchanged and not new_ids and not new_view else 0
    if stagnant >= 2 and not any(a.get('purpose') == 'disconfirm' for a in data.get('next_actions', [])):
        raise ValueError('No new evidence for this hypothesis: request a disconfirming check or change diagnosis')
    if result == 'supports_alternative':
        retired[identity] = refs
    history = record.setdefault('focus_history', [])
    if old:
        history.append({key:old.get(key) for key in ('failure','hypothesis_id','alternative','check_result','result_evidence')})
    record.update(focus=focus,refuted_hypotheses=retired,focus_stagnation=stagnant,
                  focus_evidence_seen=sorted(known),reconsideration_required=stagnant>=1,
                  focus_seen_signature=record.get('working_context_signature'))
    record.pop('focus_review_required',None)
