#!/usr/bin/env python3
# HAV-IQ @ 2026 — git-native conflict resolution (RM-GIT-CONFLICT-RESOLUTION).
#
# Runs inside the customer's CI job on a merge branch cut from the target stage branch.
# git does every merge; this script only (1) gives git a Salesforce-aware merge driver for
# keyed XML collections, (2) applies the pipeline's rules and a person's decisions with
# git's own --ours/--theirs, and (3) writes a small result file HAV-IQ reads.
#
#   python3 haviq_git_merge.py run      <request.json>       # merge (+ decisions when given)
#   python3 haviq_git_merge.py driver   %O %A %B %P          # git merge driver (installed by `run`)
#
# Exit codes (run): 0 = merged and committed, 3 = conflicts remain (result file pushed),
# 1 = could not run (message printed). Nothing is pushed on 1.
import fnmatch, json, os, re, shutil, subprocess, sys
import xml.etree.ElementTree as ET

NS = 'http://soap.sforce.com/2006/04/metadata'
SIDE_DIR = '/tmp/haviq_driver'
RESULT_PATH = 'manifest/haviq-merge-result.json'
CI_FILES = ('.gitlab-ci.yml', 'bitbucket-pipelines.yml')

# Keyed collections: element -> key child(ren). Anything not listed is a single entry by tag;
# a repeated element with no key is compared as a whole list.
KEYS = {
    'fieldPermissions': ('field',), 'objectPermissions': ('object',), 'classAccesses': ('apexClass',),
    'pageAccesses': ('apexPage',), 'tabSettings': ('tab',), 'tabVisibilities': ('tab',),
    'recordTypeVisibilities': ('recordType',), 'layoutAssignments': ('layout', 'recordType'),
    'userPermissions': ('name',), 'customPermissions': ('name',), 'applicationVisibilities': ('application',),
    'customMetadataTypeAccesses': ('name',), 'customSettingAccesses': ('name',), 'externalDataSourceAccesses': ('externalDataSource',),
    'flowAccesses': ('flow',), 'labels': ('fullName',), 'customValue': ('fullName',), 'standardValue': ('fullName',),
    'value': ('valueName',), 'matchingRules': ('fullName',), 'sharingCriteriaRules': ('fullName',),
    'sharingOwnerRules': ('fullName',), 'sharingGuestRules': ('fullName',), 'alerts': ('fullName',),
    'fieldUpdates': ('fullName',), 'outboundMessages': ('fullName',), 'rules': ('fullName',), 'tasks': ('fullName',),
    'knowledgePublishes': ('fullName',), 'send': ('fullName',), 'assignmentRule': ('fullName',),
    'autoResponseRule': ('fullName',), 'escalationRule': ('fullName',), 'fields': ('fullName',), 'listViews': ('fullName',),
    'recordTypes': ('fullName',), 'validationRules': ('fullName',), 'webLinks': ('fullName',), 'fieldSets': ('fullName',),
    'compactLayouts': ('fullName',), 'businessProcesses': ('fullName',), 'translation': ('masterLabel',),
}
# Files the driver handles (git attributes). Everything else is git's own line merge.
DRIVER_GLOBS = ('*.profile-meta.xml', '*.permissionset-meta.xml', '*.mutingpermissionset-meta.xml',
                '*.labels-meta.xml', '*.standardValueSet-meta.xml', '*.globalValueSet-meta.xml',
                '*.standardValueSetTranslation-meta.xml', '*.globalValueSetTranslation-meta.xml',
                '*.matchingRule-meta.xml', '*.sharingRules-meta.xml', '*.workflow-meta.xml',
                '*.assignmentRules-meta.xml', '*.autoResponseRules-meta.xml', '*.escalationRules-meta.xml',
                '*.object-meta.xml')
FOLDER_TYPES = {
    'classes': 'ApexClass', 'triggers': 'ApexTrigger', 'pages': 'ApexPage', 'components': 'ApexComponent',
    'lwc': 'LightningComponentBundle', 'aura': 'AuraDefinitionBundle', 'flows': 'Flow', 'layouts': 'Layout',
    'profiles': 'Profile', 'permissionsets': 'PermissionSet', 'labels': 'CustomLabels', 'objects': 'CustomObject',
    'standardValueSets': 'StandardValueSet', 'globalValueSets': 'GlobalValueSet', 'flexipages': 'FlexiPage',
    'staticresources': 'StaticResource', 'email': 'EmailTemplate', 'workflows': 'Workflow',
    'matchingRules': 'MatchingRules', 'sharingRules': 'SharingRules', 'reportTypes': 'ReportType',
    'quickActions': 'QuickAction', 'tabs': 'CustomTab', 'applications': 'CustomApplication',
}


def git(*args, check=True, capture=True):
    r = subprocess.run(['git'] + list(args), capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError('git ' + ' '.join(args) + ' failed: ' + (r.stderr or r.stdout).strip()[:400])
    return r.stdout if capture else ''


def type_of(path):
    parts = path.split('/')
    if 'objects' in parts:
        i = parts.index('objects')
        if len(parts) > i + 2:
            return {'fields': 'CustomField', 'validationRules': 'ValidationRule', 'listViews': 'ListView',
                    'recordTypes': 'RecordType', 'webLinks': 'WebLink'}.get(parts[i + 2], 'CustomObject')
    for p in parts:
        if p in FOLDER_TYPES:
            return FOLDER_TYPES[p]
    return 'Other'


# ── XML driver ────────────────────────────────────────────────────────────────────────────
def local(tag):
    return tag.split('}', 1)[1] if '}' in tag else tag


def canon(el):
    def strip(e):
        e.tail = None
        if e.text is not None and not e.text.strip():
            e.text = None
        for c in e:
            strip(c)
    import copy
    c = copy.deepcopy(el)
    strip(c)
    return ET.tostring(c, encoding='unicode')


def key_of(el, counts):
    t = local(el.tag)
    if t in KEYS:
        vals = []
        for k in KEYS[t]:
            ch = el.find('{%s}%s' % (NS, k))
            if ch is None:
                ch = el.find(k)
            vals.append((ch.text or '').strip() if ch is not None else '')
        return (t,) + tuple(vals)
    counts[t] = counts.get(t, 0) + 1
    return (t, '#%d' % counts[t])


def entries(root):
    out, counts, order = {}, {}, []
    for ch in list(root):
        k = key_of(ch, counts)
        if k in out:                       # duplicate key: not safely mergeable by key
            raise ValueError('duplicate entry ' + '/'.join(k))
        out[k] = ch
        order.append(k)
    return out, order


def xml_merge(base_p, ours_p, theirs_p):
    """Returns (merged_root or None, conflicts[list of key strings], tree for writing)."""
    ET.register_namespace('', NS)
    parse = lambda p: ET.parse(p) if os.path.getsize(p) > 0 else None
    tb, to, tt = parse(base_p), parse(ours_p), parse(theirs_p)
    if to is None or tt is None:
        raise ValueError('empty side')
    rb = tb.getroot() if tb is not None else ET.Element(to.getroot().tag)
    ro, rt = to.getroot(), tt.getroot()
    if local(ro.tag) != local(rt.tag):
        raise ValueError('different root elements')
    eb, _ = entries(rb)
    eo, order_o = entries(ro)
    et, order_t = entries(rt)
    conflicts, chosen = [], {}
    for k in set(eb) | set(eo) | set(et):
        b = canon(eb[k]) if k in eb else None
        o = canon(eo[k]) if k in eo else None
        t = canon(et[k]) if k in et else None
        if o == t:
            chosen[k] = eo.get(k)
        elif o == b:
            chosen[k] = et.get(k)
        elif t == b:
            chosen[k] = eo.get(k)
        else:
            conflicts.append(k)
            chosen[k] = eo.get(k)
    # Output order: ours' order, theirs-only additions after the last sibling of the same element.
    result = [k for k in order_o if chosen.get(k) is not None]
    added = sorted([k for k in order_t if k not in eo and chosen.get(k) is not None], key=lambda k: k)
    for k in added:
        pos = max([i for i, x in enumerate(result) if x[0] == k[0]], default=len(result) - 1)
        result.insert(pos + 1, k)
    for ch in list(ro):
        ro.remove(ch)
    for k in result:
        ro.append(chosen[k])
    return ro, conflicts, to


def driver(o, a, b, path):
    os.makedirs(SIDE_DIR, exist_ok=True)
    side = os.path.join(SIDE_DIR, re.sub(r'[^A-Za-z0-9._-]', '_', path) + '.json')
    try:
        root, conflicts, tree = xml_merge(o, a, b)
    except Exception as e:                  # cannot be sure: hand back to git's line merge
        r = subprocess.run(['git', 'merge-file', '-L', 'target', '-L', 'base', '-L', 'source', a, o, b])
        json.dump({'engine': 'git', 'reason': 'xml fallback: ' + str(e)[:200]}, open(side, 'w'))
        return 0 if r.returncode == 0 else 1
    if conflicts:
        json.dump({'engine': 'haviq-xml', 'entries': ['/'.join(k) for k in sorted(conflicts)]}, open(side, 'w'))
        return 1                            # leave ours in %A; git marks the path unmerged
    ET.indent(tree, space='    ')
    with open(a, 'w', encoding='utf-8') as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write(ET.tostring(root, encoding='unicode'))
        fh.write('\n')
    json.dump({'engine': 'haviq-xml', 'entries': []}, open(side, 'w'))
    return 0


# ── the job ───────────────────────────────────────────────────────────────────────────────
BINARY_EXT = ('.zip', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.ico', '.woff', '.woff2', '.ttf', '.eot', '.jar', '.gz')


def last_change(ref, path):
    """Who last changed path on ref: author, ISO date, subject (the Work Item / ticket usually lives there)."""
    out = git('log', '-1', '--format=%an%x1f%aI%x1f%s', ref, '--', path, check=False).strip()
    if not out:
        return None
    a, d, s = (out.split('\x1f') + ['', '', ''])[:3]
    return {'author': a, 'date': d, 'subject': s[:200]}


def blob_size(sha):
    if not sha:
        return None
    try:
        return int(git('cat-file', '-s', sha).strip())
    except Exception:
        return None


def is_binary(path, sha):
    if path.lower().endswith(BINARY_EXT):
        return True
    if not sha:
        return False
    head = subprocess.run(['git', 'cat-file', 'blob', sha], capture_output=True).stdout[:8000]
    return b'\0' in head


def rule_for(path, rules):
    t = type_of(path)
    for r in rules or []:
        m = r.get('match', '')
        if m == t or fnmatch.fnmatch(path, m):
            return r.get('rule', 'merge')
    return 'merge'


def unmerged():
    out = {}
    for line in git('ls-files', '-u', '-z').split('\0'):
        if not line.strip():
            continue
        meta, path = line.split('\t', 1)
        mode, sha, stage = meta.split()
        out.setdefault(path, {})[{'1': 'base', '2': 'target', '3': 'source'}[stage]] = sha
    return out


def run(request_path):
    req = json.load(open(request_path))
    wi = req['workItemBranch']
    if not re.fullmatch(r'[A-Za-z0-9._/-]+', wi or '') or '..' in wi:
        print('[haviq] The request names an unsafe branch; stopping.')
        return 1
    rules, decisions = req.get('rules') or [], req.get('decisions') or {}
    shutil.rmtree(SIDE_DIR, ignore_errors=True)
    me = os.path.abspath(__file__)
    git('config', 'user.email', 'ci@haviq.local')
    git('config', 'user.name', 'HAV-IQ CI')
    git('config', 'merge.haviq-xml.name', 'HAV-IQ Salesforce XML merge')
    git('config', 'merge.haviq-xml.driver', 'python3 "%s" driver %%O %%A %%B %%P' % me)
    os.makedirs('.git/info', exist_ok=True)
    with open('.git/info/attributes', 'w') as fh:
        fh.write(''.join('%s merge=haviq-xml\n' % g for g in DRIVER_GLOBS))
    git('fetch', '--unshallow', 'origin', check=False)
    git('fetch', 'origin', '+refs/heads/%s:refs/remotes/origin/%s' % (wi, wi))
    base = git('merge-base', 'HEAD', 'origin/' + wi).strip()
    both = set(git('diff', '--name-only', base, 'HEAD').split()) & set(git('diff', '--name-only', base, 'origin/' + wi).split())
    files = len(set(git('diff', '--name-only', base, 'origin/' + wi).split()))
    subprocess.run(['git', 'merge', '--no-ff', '--no-commit', 'origin/' + wi], capture_output=True, text=True)
    left = unmerged()
    policy, decided, ci_kept = [], [], []
    for path in list(left):
        if path in CI_FILES or path.startswith('.github/workflows/'):
            git('checkout', '--ours', '--', path); git('add', '--', path); left.pop(path); ci_kept.append(path); continue
        choice = decisions.get(path) or ({'source': 'source', 'target': 'target'}.get(rule_for(path, rules)))
        if not choice:
            continue
        if choice == 'source':
            if 'source' in left[path]: git('checkout', '--theirs', '--', path); git('add', '--', path)
            else: git('rm', '-q', '--', path)
        elif choice == 'target':
            if 'target' in left[path]: git('checkout', '--ours', '--', path); git('add', '--', path)
            else: git('rm', '-q', '--', path)
        elif choice == 'edited':
            src = os.path.join('manifest', 'haviq-edits', path)
            if not os.path.exists(src):
                continue
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            open(path, 'wb').write(open(src, 'rb').read()); git('add', '--', path)
        else:
            continue
        (decided if path in decisions else policy).append({'path': path, 'choice': choice})
        left.pop(path)
    # "Always ask": a file both sides changed is held for a person even when git merged it.
    held = [p for p in sorted(both) if p not in left and rule_for(p, rules) == 'ask' and p not in decisions]
    remaining = []
    for path, stages in sorted(left.items()):
        side = os.path.join(SIDE_DIR, re.sub(r'[^A-Za-z0-9._-]', '_', path) + '.json')
        info = json.load(open(side)) if os.path.exists(side) else {'engine': 'git'}
        kind = 'add/add' if 'base' not in stages else ('delete/modify' if len(stages) < 3 else 'content')
        remaining.append(dict(path=path, type=type_of(path), kind=kind, engine=info.get('engine', 'git'),
                              entries=info.get('entries', []), base=stages.get('base'),
                              source=stages.get('source'), target=stages.get('target'),
                              binary=is_binary(path, stages.get('source') or stages.get('target')),
                              sizes={k: blob_size(stages.get(k)) for k in ('base', 'source', 'target')},
                              sourceChange=last_change('origin/' + wi, path), targetChange=last_change('HEAD', path)))
    for p in held:
        remaining.append(dict(path=p, type=type_of(p), kind='review', engine='rule', entries=[], binary=False,
                              sourceChange=last_change('origin/' + wi, p), targetChange=last_change('HEAD', p),
                              base=git('rev-parse', '%s:%s' % (base, p), check=False).strip() or None,
                              source=git('rev-parse', 'origin/%s:%s' % (wi, p), check=False).strip() or None,
                              target=git('rev-parse', 'HEAD:%s' % p, check=False).strip() or None))
    result = {'summary': {'files': files, 'bothChanged': len(both), 'policyResolved': len(policy),
                          'decided': len(decided), 'conflicts': len(remaining)},
              'policy': policy, 'conflicts': remaining, 'workItemBranch': wi,
              'targetHead': git('rev-parse', 'HEAD').strip(), 'sourceHead': git('rev-parse', 'origin/' + wi).strip()}
    if remaining:
        # Back to the merge branch as it was (a CI clone: nothing else to keep), then record only the result.
        git('merge', '--abort', check=False)
        git('reset', '-q', '--hard', 'HEAD')
        os.makedirs('manifest', exist_ok=True)
        json.dump(result, open(RESULT_PATH, 'w'), indent=1)
        git('add', '--', RESULT_PATH)
        git('rm', '-q', '--cached', '--ignore-unmatch', '--', 'manifest/haviq-merge.json', 'manifest/haviq-merge.py', check=False)
        git('commit', '-q', '-m', 'HAV-IQ: %d conflict(s) need a decision [haviq-merge:conflict] [skip ci]' % len(remaining))
        print('[haviq] %d file(s) need a decision; result written to %s.' % (len(remaining), RESULT_PATH))
        return 3
    helped = [f for f in (os.listdir(SIDE_DIR) if os.path.isdir(SIDE_DIR) else [])
              if json.load(open(os.path.join(SIDE_DIR, f))).get('engine') == 'haviq-xml']
    result['summary']['xmlMerged'] = len(helped)
    if not (helped or policy or decided or ci_kept):
        # git merged everything on its own: the provider's merge request will do the same, so the
        # deployment keeps its Work Item branch. Only the request is removed here.
        git('merge', '--abort', check=False)
        git('reset', '-q', '--hard', 'HEAD')
        for f in ('manifest/haviq-merge.json', 'manifest/haviq-merge.py', RESULT_PATH):
            git('rm', '-q', '--ignore-unmatch', '--', f, check=False)
        git('rm', '-q', '-r', '--ignore-unmatch', '--', 'manifest/haviq-edits', check=False)
        git('commit', '-q', '--allow-empty', '-m', 'HAV-IQ: merge check clean [haviq-merge:clean] [skip ci]')
        json.dump(result, open('/tmp/haviq_merge_result.json', 'w'))
        print('[haviq] git merges %s cleanly on its own (%d file(s)); nothing to push but this check.' % (wi, files))
        return 0
    # The merge commit carries no HAV-IQ working files: request, script, edits, an earlier result.
    for f in ('manifest/haviq-merge.json', 'manifest/haviq-merge.py', RESULT_PATH):
        git('rm', '-q', '--cached', '--ignore-unmatch', '--', f, check=False)
    git('rm', '-q', '-r', '--cached', '--ignore-unmatch', '--', 'manifest/haviq-edits', check=False)
    subprocess.run(['rm', '-rf', 'manifest/haviq-edits', RESULT_PATH, 'manifest/haviq-merge.json', 'manifest/haviq-merge.py'])
    msg = (req.get('commitMessage') or 'HAV-IQ: merge %s' % wi) + ' [haviq-merge:merged]'
    git('commit', '-q', '-m', msg)
    json.dump(result, open('/tmp/haviq_merge_result.json', 'w'))
    print('[haviq] Merged %s: %d file(s), %d changed on both sides, %d by rules, %d by decision, 0 left.'
          % (wi, files, len(both), len(policy), len(decided)))
    return 0


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'driver':
        sys.exit(driver(*sys.argv[2:6]))
    if len(sys.argv) >= 3 and sys.argv[1] == 'run':
        try:
            sys.exit(run(sys.argv[2]))
        except Exception as e:
            print('[haviq] The merge job could not run: ' + str(e)[:500])
            sys.exit(1)
    print(__doc__ or 'usage: haviq_git_merge.py run <request.json>')
    sys.exit(1)
