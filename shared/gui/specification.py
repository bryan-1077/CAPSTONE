"""GUI interaction around existing frontend extraction and validation functions."""
from dataclasses import replace
import hashlib
import importlib
import json
import sys
from .run_reader import REPO_ROOT


def frontend_api():
    path = str(REPO_ROOT / 'local/frontend')
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module('configure_from_text')


def select_specification(description, directory, interaction, api=None):
    import yaml
    api = api or frontend_api()
    warnings = rejections = revision = 0
    while warnings < api.MAX_CLARIFICATION_WARNINGS:
        print('Interpreting design description…', flush=True)
        try:
            candidate, assumptions, preset, _ = api.normalize_result(
                api.extract_config_with_llm(description, api.DEFAULT_LLM_MODEL))
        except api.FatalConfigTextError:
            raise
        except api.ConfigTextError as exc:
            warnings += 1
            if warnings >= api.MAX_CLARIFICATION_WARNINGS:
                raise ValueError('Specification clarification limit reached') from exc
            response = interaction.request('description', dict(description=description,
                reason=str(exc), remaining_attempts=api.MAX_CLARIFICATION_WARNINGS - warnings))
            description = str(response.get('value', '')).strip()
            if not description:
                raise ValueError('Description must not be empty')
            continue
        revision += 1
        rendered = yaml.safe_dump(candidate, sort_keys=False)
        digest = hashlib.sha256(rendered.encode()).hexdigest()
        validation_error = ''
        while True:
            response = interaction.request('spec_approval', dict(candidate=rendered,
                assumptions=assumptions, preset=preset, digest=digest, revision=revision,
                description=description, validation_error=validation_error))
            if response.get('action') != 'approve' or response.get('digest') != digest:
                break
            edited = response.get('configuration', candidate)
            try:
                if not isinstance(edited, dict):
                    raise ValueError('Configuration must be a mapping')
                if edited != candidate:
                    api.validate_user_config(edited)
            except ValueError as exc:
                validation_error = str(exc)
                continue
            break
        if response.get('action') == 'approve' and response.get('digest') == digest:
            approved = response.get('configuration', candidate)
            if not isinstance(approved, dict):
                raise ValueError('Approved configuration must be a mapping')
            rendered = yaml.safe_dump(approved, sort_keys=False)
            approved_digest = hashlib.sha256(rendered.encode()).hexdigest()
            path = directory / 'gui_approved_specs.yaml'
            path.write_text(rendered, encoding='utf-8')
            (directory / 'gui_spec_approval.json').write_text(json.dumps(dict(
                description=description, assumptions=assumptions, preset=preset,
                digest=approved_digest, proposed_digest=digest, edited=approved != candidate,
                revision=revision, approved=True), indent=2) + '\n')
            return path
        if response.get('action') != 'revise':
            raise ValueError('Approval does not match the proposed specification')
        rejections += 1
        if rejections >= api.MAX_CONFIRMATION_REJECTIONS:
            raise ValueError('Specification revision limit reached')
        description = str(response.get('value', '')).strip()
        if not description:
            raise ValueError('Revised description must not be empty')
    raise ValueError('No approved specification')


def review_frequency(config, interaction, directory=None):
    import yaml
    from shared.top_orchestrator.frequency import positive_mhz, clock_requirements, ask_advisor
    text = config.input_yaml.read_text()
    requirements = clock_requirements(yaml.safe_load(text))
    conversation = []
    while True:
        response = interaction.request('frequency', dict(target_mhz=config.target_mhz,
            requirements=requirements, conversation=conversation))
        if response.get('action') == 'discuss':
            conversation.append(dict(role='user', content=str(response.get('value', 'Help me choose a target.'))))
            try:
                advice = ask_advisor(text, conversation)
            except Exception:
                advice = 'Advisor unavailable. You can still select a target explicitly.'
            conversation.append(dict(role='assistant', content=advice))
            continue
        target = positive_mhz(str(response.get('value', '')))
        if directory is not None:
            (directory / 'gui_frequency_review.json').write_text(json.dumps(dict(
                target_mhz=target, requirements=requirements, conversation=conversation), indent=2) + '\n')
        return replace(config, target_mhz=target, target_mhz_source='GUI selection')
