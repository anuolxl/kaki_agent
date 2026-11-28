"""Read-only metadata probe for the SSH target fixed by trusted local configuration."""

import asyncio
import json
import os
import re
import shlex
import sys

from kaki.execution import run_process

REMOTE = r"""
import json,os,pathlib,platform,shutil,subprocess
def command(argv):
 try:
  r=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=12)
  return {"exit_code":r.returncode,"stdout":r.stdout[:1200],"stderr":r.stderr[:300]}
 except Exception as e:return {"error":type(e).__name__}
home=pathlib.Path.home()
roots=[home, pathlib.Path('/opt/tiger'),pathlib.Path('/workspace'),pathlib.Path('/data')]
names={'byteview-mp-cpp-sdk','byteview-mp-media_platform'}
skip={'.git','.cache','node_modules','pkg','cpp3rdlib','.bpt','.local','build','output','.vscode-server','lost+found'}
repos=[];seen=set();visited=0
for root in roots:
 if not root.is_dir():continue
 for current,dirs,files in os.walk(str(root)):
  visited+=1;p=pathlib.Path(current)
  depth=len(p.relative_to(root).parts)
  dirs[:]=[d for d in dirs if d not in skip and not d.startswith('.')]
  if depth>=6 or visited>3000:dirs[:]=[]
  if p.name in names and (p/'.git').exists() and str(p) not in seen:
   seen.add(str(p));repos.append({'name':p.name,'path':str(p),'branch':command(['git','-C',str(p),'branch','--show-current']),'head':command(['git','-C',str(p),'rev-parse','HEAD']),'tracked_status':command(['git','-C',str(p),'status','--porcelain','--untracked-files=no'])});dirs[:]=[]
  if visited>3000:break
tools={n:shutil.which(n) for n in ['git','go','blade','bpt','clang++','g++','cmake','ninja','pkg-config','docker','podman']}
blade_paths=[str(pathlib.Path(d)/'blade') for d in os.environ.get('PATH','').split(os.pathsep) if d]+[str(home/'.blade_tools'/'blade'),str(home/'.local/bin/blade'),'/opt/tiger/ss_bin/blade','/opt/tiger/typhoon-blade/blade','/usr/local/bin/blade']
blades=[]
for value in sorted(set(blade_paths)):
 if not pathlib.Path(value).is_file():continue
 try:
  r=subprocess.run([value,'build','--help'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=15)
  text=r.stdout+'\n'+r.stderr;blades.append({'path':value,'exit_code':r.returncode,'supports_bpt':'--enable-bpt' in text,'help_excerpt':text[:300]})
 except Exception as e:blades.append({'path':value,'error':type(e).__name__})
report={'hostname':platform.node(),'user':os.environ.get('USER'),'home':str(home),'system':platform.system(),'machine':platform.machine(),'tools':tools,'go_version':command(['go','version']) if tools['go'] else None,'blade_candidates':blades,'docker_info':command(['docker','info','--format','{{.ServerVersion}}']) if tools['docker'] else None,'libraries':{str(p):p.is_dir() for p in [pathlib.Path('/opt/tiger/byteview/rtc/worker/lib'),home/'.blade_tools']},'repositories':repos,'scan_limit_reached':visited>3000,'dedicated_root':str(home/'.kaki-devbox'/'workspaces')}
print('KAKI_DEVBOX_JSON:'+json.dumps(report,ensure_ascii=False))
"""


async def main():
    # 目标取自本机可信配置；SSH仅执行固定元数据脚本，标记行用于隔离远端Shell启动噪声。
    target = os.environ.get("KAKI_DEVBOX_TARGET", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+", target):
        raise SystemExit("KAKI_DEVBOX_TARGET必须由本机配置指定user@host")
    remote = shlex.join(["bash", "-lc", shlex.join(["python3", "-c", REMOTE])])
    result = await run_process(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=accept-new",
            target,
            remote,
        ],
        __import__("pathlib").Path.cwd(),
        90,
        {"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]} if os.environ.get("SSH_AUTH_SOCK") else {},
    )
    marker = "KAKI_DEVBOX_JSON:"
    line = next(
        (line[len(marker) :] for line in result["stdout"].splitlines() if line.startswith(marker)), None
    )
    if line:
        print(
            json.dumps(
                {"target": target, "report": json.loads(line), "exit_code": result["exit_code"]},
                ensure_ascii=False,
            )
        )
    else:
        print(json.dumps(result, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
