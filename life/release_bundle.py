"""Multi-file candidate snapshots; the running independent controller owns activation."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid


def paths(manifest):
    return manifest.get('modified_paths') or [manifest['modified']]


def prepare(controller,proposal_id,patch):
    changes = patch.get('changes',[patch] if 'file' in patch else [])
    if not isinstance(changes,list) or (not changes and not patch.get('migrations')):
        raise ValueError('empty_or_invalid_code_changes')
    originals = {p.relative_to(controller.source).as_posix():p for p in controller._files()}
    updates = {}
    seen = set()
    for item in changes:
        path = (controller.source/str(item['file']).replace('\\','/')).resolve()
        relative = path.relative_to(controller.source).as_posix()
        if (relative.casefold() in seen or relative.casefold() in {p.casefold() for p in controller.PROTECTED}
                or relative.casefold().startswith(('audit/','tests/','data/','.git/'))
                or path.name.casefold().endswith('test.py') or path.name.casefold().startswith('test_')
                or path.suffix!='.py' or (relative not in originals and not (
                    path.parent==controller.source or relative.startswith('life/')))):
            raise PermissionError('protected_or_unknown_candidate_path')
        seen.add(relative.casefold())
        if path.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        action = item.get('action','replace')
        if action=='create':
            if path.exists():
                raise ValueError('new_file_already_exists')
            content = str(item['content'])
        else:
            if relative not in originals:
                raise ValueError('patch_source_missing')
            text = path.read_text('utf-8')
            if action in {'write','delete'}:
                if item.get('base_hash')!=controller.digest(path):
                    raise ValueError('patch_base_mismatch')
                content = None if action=='delete' else str(item['content'])
            elif action=='replace':
                old,new = str(item['old']),str(item['new'])
                if not old or text.count(old)!=1:
                    raise ValueError('patch_base_mismatch')
                content = text.replace(old,new,1)
            else:
                raise ValueError('unknown_patch_action')
        if content is not None:
            compile(content,relative,'exec')
        updates[relative] = content
    from .release_migrations import validate
    migrations = patch.get('migrations',[])
    validate(migrations)
    identity = 'candidate-'+uuid.uuid4().hex
    root = controller.artifacts/identity
    hashes = {key:controller.digest(path) for key,path in originals.items()}
    for kind in ('baseline','candidate'):
        destination = root/kind
        destination.mkdir(parents=True)
        for key,path in originals.items():
            target = destination/key
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
    candidate = dict(hashes)
    for key,content in updates.items():
        target = root/'candidate'/key
        if content is None:
            target.unlink()
            candidate.pop(key)
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(content,encoding='utf-8',newline='\n')
            candidate[key] = controller.digest(target)
    fixtures = {}
    if (controller.source/'bot.py').is_file():
        fixture = b'# Offline test persona\nAn offline fixture without private profile content.\n'
        for kind in ('baseline','candidate'):
            (root/kind/'persona.md').write_bytes(fixture)
        fixtures['persona.md'] = hashlib.sha256(fixture).hexdigest()
    modified = list(updates)
    manifest = dict(proposal_id=proposal_id,baseline=hashes,candidate=candidate,fixtures=fixtures,
                    modified=modified[0] if modified else '',modified_paths=modified,migrations=migrations)
    with controller._db() as db,db:
        db.execute("INSERT INTO candidates VALUES(?,?,'prepared','{}')",(identity,json.dumps(manifest)))
    return identity


def current_hashes(controller,manifest):
    files = set(controller._files())|{controller.source/p for p in paths(manifest) if p}
    return {p.relative_to(controller.source).as_posix():controller.digest(p) for p in files if p.is_file()}


def install(controller,identity,database=None):
    deployment = controller.deployment(identity)
    if not deployment or deployment['phase'] not in {'queued','prepared','installed'}:
        raise PermissionError('deployment_not_prepared')
    manifest,phase,_ = controller._candidate(identity)
    if phase!='publishable':
        raise PermissionError('evaluation_not_publishable')
    controller._verify(identity,manifest,source=False)
    from supervise import validate_data_compatibility
    validate_data_compatibility(controller.artifacts/identity/'candidate',database=database or controller.source/'data/life.db')
    modified = set(paths(manifest))
    actual = current_hashes(controller,manifest)
    expected = set(manifest['baseline'])|set(manifest['candidate'])
    if set(actual)-expected or any(actual.get(p)!=h for p,h in manifest['baseline'].items() if p not in modified):
        raise ValueError('production_base_changed')
    for p in modified:
        if actual.get(p) not in {manifest['baseline'].get(p),manifest['candidate'].get(p)}:
            raise ValueError('production_base_changed')
    from .release_migrations import apply
    if manifest.get('migrations'):
        if database is None:
            raise ValueError('migration_requires_bound_life_database')
        apply(database,manifest['migrations'])
    controller.deployment_phase(identity,'prepared')
    for p in paths(manifest):
        if not p:
            continue
        target = controller.source/p
        desired = manifest['candidate'].get(p)
        if (controller.digest(target) if target.is_file() else None)==desired:
            continue
        if desired is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            controller._replace_file(target,(controller.artifacts/identity/'candidate'/p).read_bytes())
    if current_hashes(controller,manifest)!=manifest['candidate']:
        raise ValueError('installed_bundle_mismatch')
    controller.deployment_phase(identity,'installed')


def rollback(controller,identity):
    manifest,_,_ = controller._candidate(identity)
    from supervise import validate_data_compatibility
    deployment = controller.deployment(identity) or {}
    origin = json.loads(deployment.get('origin') or '{}')
    validate_data_compatibility(controller.artifacts/identity/'baseline',database=origin.get('database') or controller.source/'data/life.db')
    # Check every path before changing any, including files created by this bundle.
    for p in paths(manifest):
        if not p:
            continue
        target = controller.source/p
        current = controller.digest(target) if target.is_file() else None
        if current not in {manifest['baseline'].get(p),manifest['candidate'].get(p)}:
            controller.deployment_phase(identity,'conflict','user_changed_deployed_file')
            raise ValueError('rollback_would_overwrite_user_change')
    for p in paths(manifest):
        if not p:
            continue
        target = controller.source/p
        if p not in manifest['baseline']:
            target.unlink(missing_ok=True)
        else:
            baseline = controller.artifacts/identity/'baseline'/p
            if controller.digest(baseline)!=manifest['baseline'][p]:
                raise ValueError('rollback_artifact_changed')
            target.parent.mkdir(parents=True,exist_ok=True)
            controller._replace_file(target,baseline.read_bytes())
    controller.deployment_phase(identity,'rolled_back')
