"""Fixed immutable source reads; trust applies only to one verified repository."""
from pathlib import Path
import re
import stat
from time import monotonic


def snapshot(repo):
    started=monotonic(); result={}
    nodes=[repo,*repo.parents]
    pending=[repo]
    while pending:
        node=pending.pop()
        nodes.append(node)
        info=node.lstat()
        if stat.S_ISDIR(info.st_mode):pending.extend(node.iterdir())
        if len(nodes)>100000 or monotonic()-started>5:raise ValueError('source proof incomplete')
    for node in nodes:
        info=node.lstat()
        if (info.st_uid!=0 or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise ValueError('immutable source required')
        # Reads may update atime; ownership, content and replacement metadata must stay fixed.
        result[str(node)]=(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
                           info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    if not stat.S_ISDIR((repo/'.git').lstat().st_mode):raise ValueError('local git control required')
    # External includes/alternate object stores fall outside this immutable proof.
    for relative in ('.git/commondir','.git/objects/info/alternates','.git/info/grafts'):
        if (repo/relative).exists():raise ValueError('external source control unsupported')
    config=repo/'.git/config'
    if config.exists():
        from configparser import ConfigParser
        parser=ConfigParser(allow_no_value=True,interpolation=None)
        if config.stat().st_size>65536:raise ValueError('source config too large')
        parser.read_string(config.read_text())
        if any(section.lower().startswith('include') for section in parser.sections()):raise ValueError('external source config unsupported')
        if parser.has_option('core','worktree'):raise ValueError('foreign worktree unsupported')
    return result


def read(profile,operation,run):
    sha=profile['release']['expected_sha'];repo=Path(profile['release']['root'])/'repo'
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('pinned source required')
    if not (operation==['ls-tree','-r','--name-only',sha,'src/lp_v2'] or
            (len(operation)==2 and operation[0]=='show' and
             re.fullmatch(re.escape(sha)+r':(?:deploy/run-live|src/lp_v2/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py)',operation[1]))):
        raise ValueError('source operation refused')
    before=snapshot(repo)
    prefix=['git','-c','safe.directory='+str(repo),'-C',str(repo)]
    origin=run(prefix+['config','--get','remote.origin.url'])
    resolved=run(prefix+['rev-parse','--verify',sha+'^{commit}'])
    if (origin.returncode or origin.stdout.strip() not in ('https://github.com/raymizzou/lp-v2.git','git@github.com:raymizzou/lp-v2.git')
            or resolved.returncode or resolved.stdout.strip()!=sha):raise ValueError('source identity differs')
    result=run(prefix+operation)
    if before!=snapshot(repo):raise ValueError('source changed')
    return result
