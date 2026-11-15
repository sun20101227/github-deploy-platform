"""In-place updates for explicitly imported repositories; never move live data."""
import base64
import os
import subprocess
from pathlib import Path
from git import Repo
from . import cli_auth, db, discovery


def execute(job, account, project, action, note, start, run):
    root = Path(project['deploy_root'])
    changed = False
    before = target = ''
    with Repo(root) as repo:
        try:
            if repo.is_dirty(untracked_files=True):
                raise RuntimeError('目录有本地修改或未跟踪文件，请先处理后更新')
            if repo.head.is_detached or repo.active_branch.name != project['branch']:
                raise RuntimeError('本地分支已改变，请先确认项目配置')
            if discovery.repository_name(repo.remotes.origin.url) != project['repository']:
                raise RuntimeError('origin 已改变，请重新确认仓库')
            if repo.submodules:
                raise RuntimeError('含子模块的已有项目需要使用自定义部署流程')
            before = repo.head.commit.hexsha
            with db.connect() as c:
                c.execute("UPDATE deployments SET status='running',previous_commit=?,release_dir=? WHERE id=?",(before,str(root),job))
            if action == 'rollback':
                with db.connect() as c:
                    row=c.execute("SELECT previous_commit,commit_sha FROM deployments WHERE project_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",(project['id'],)).fetchone()
                if not row or not row['previous_commit'] or row['commit_sha'] != before:
                    raise RuntimeError('当前提交不符合回滚记录')
                target = row['previous_commit']
                repo.git.reset('--keep', target)
            else:
                note(job, '拉取远端提交')
                token=cli_auth.token(account['access_token'])
                auth=base64.b64encode(('x-access-token:'+token).encode()).decode()
                env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GIT_CONFIG_COUNT='1',
                         GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',GIT_CONFIG_VALUE_0='Authorization: Basic '+auth)
                # Use gh credentials even when the original remote uses SSH. Do not rewrite origin.
                subprocess.run(['git','fetch','--','https://github.com/'+project['repository']+'.git',project['branch']],
                               cwd=root,env=env,timeout=180,check=True,capture_output=True)
                target=repo.commit('FETCH_HEAD').hexsha
                if not repo.is_ancestor(before,target):
                    raise RuntimeError('本地与远端已分叉，无法快进更新')
                repo.git.merge('--ff-only','--no-overwrite-ignore',target)
            changed = before != repo.head.commit.hexsha
            with db.connect() as c:
                c.execute('UPDATE deployments SET commit_sha=? WHERE id=?',(target,job))
            if project['build_command']:
                cmd=project['build_command']
                note(job,'执行构建')
                run(['cmd','/c',cmd] if os.name=='nt' else ['sh','-c',cmd],cwd=root,timeout=1800)
            note(job,'重启服务并检查状态')
            start(project,root)
            with db.connect() as c:
                c.execute("UPDATE deployments SET status='succeeded',finished_at=? WHERE id=?",(db.now(),job))
            note(job,'更新与重启完成')
        except Exception as exc:
            # Git errors can echo URLs/headers. Return only intentional application errors.
            note(job,str(exc) if type(exc) is RuntimeError else '更新或重启失败，请检查本机 Git 和服务日志')
            status='failed'
            if changed:
                try:
                    if repo.head.commit.hexsha != target or repo.is_dirty(untracked_files=True):
                        raise RuntimeError('目录在部署期间发生修改')
                    repo.git.reset('--keep',before)
                    # Rebuild old code where required before restarting it.
                    if project['build_command']:
                        cmd=project['build_command']
                        run(['cmd','/c',cmd] if os.name=='nt' else ['sh','-c',cmd],cwd=root,timeout=1800)
                    start(project,root)
                    status='rolled_back'
                    note(job,'已恢复旧提交并重启')
                except Exception:
                    status='rollback_failed'
                    note(job,'恢复未完成，请在服务器检查，未强制覆盖本地文件')
            with db.connect() as c:
                c.execute('UPDATE deployments SET status=?,finished_at=? WHERE id=?',(status,db.now(),job))
