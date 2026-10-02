#!/usr/bin/env python3
"""Explicit guest-only lifecycle and disposable runtime quota proof."""
from __future__ import annotations
import argparse
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import shutil
import time
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLASS = 'ok-storage-local-quota'
NAMESPACE = 'ok-storage-local-quota'
BASE = '/var/openebs/local-quota'
SIZE = 64 * 1024 * 1024

class Failure(RuntimeError):
    pass

def require(ok, rule):
    if not ok:
        raise Failure(rule)

def run(cmd, data=None):
    p = subprocess.run(cmd, input=data, text=True, capture_output=True, cwd=ROOT, timeout=240)
    require(p.returncode == 0, f'command failed: {cmd[0]}: {p.stderr.strip() or p.stdout.strip()}')
    return p.stdout

def k(args, *cmd, data=None):
    return run(['kubectl', '--kubeconfig', args.kubeconfig, '--request-timeout=210s', *cmd], data)

def get(args, *cmd):
    return json.loads(k(args, 'get', *cmd, '-o', 'json'))

def manifest_check():
    pin = json.loads((ROOT/'demo/local-quota-pin.json').read_text())
    archive = ROOT/'demo/local-quota-chart-4.6.0.tgz'
    require(hashlib.sha256(archive.read_bytes()).hexdigest() == pin['sha256'], 'chart SHA256')
    for filename, key in [('demo/local-quota-provisioner.yaml', 'provisioner_sha256'), ('demo/ok-storage-local-quota.yaml', 'class_sha256'), ('values/local-quota-values.yaml', 'values_sha256')]:
        require(hashlib.sha256((ROOT/filename).read_bytes()).hexdigest() == pin[key], 'pinned file drift: '+filename)
    require(pin['version'] == '4.6.0' and pin['url'] == 'https://openebs.github.io/dynamic-localpv-provisioner/localpv-provisioner-4.6.0.tgz', 'chart pin')
    docs = list(yaml.safe_load_all((ROOT/'demo/local-quota-provisioner.yaml').read_text()))
    sc = yaml.safe_load((ROOT/'demo/ok-storage-local-quota.yaml').read_text())
    check_class(sc)
    ns = next(d for d in docs if d and d['kind'] == 'Namespace')
    require(ns['metadata']['name'] == NAMESPACE and ns['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'privileged', 'privileged namespace')
    deploy = next(d for d in docs if d and d['kind'] == 'Deployment')
    require(deploy['metadata']['namespace'] == NAMESPACE, 'deployment namespace')
    require(any(e.get('name') == 'OPENEBS_IO_BASE_PATH' and e.get('value') == BASE for c in deploy['spec']['template']['spec']['containers'] for e in c.get('env', [])), 'deployment BasePath')
    return deploy['metadata']['name']

def check_class(sc):
    require(sc.get('metadata', {}).get('name') == CLASS, 'StorageClass name')
    require(sc.get('provisioner') == 'openebs.io/local', 'StorageClass provisioner')
    require(sc.get('reclaimPolicy') == 'Delete', 'StorageClass reclaim')
    require(sc.get('volumeBindingMode') == 'WaitForFirstConsumer', 'StorageClass binding')
    require(sc.get('allowVolumeExpansion', False) is False, 'fixed capacity')
    require(sc.get('metadata', {}).get('annotations', {}).get('storageclass.kubernetes.io/is-default-class', 'false') == 'false', 'nondefault class')
    config = yaml.safe_load(sc['metadata']['annotations']['cas.openebs.io/config'])
    values = {i['name']: i.get('value') for i in config}
    quota = next((i for i in config if i['name'] == 'XFSQuota'), {})
    require(values.get('StorageType') == 'hostpath' and values.get('BasePath') == BASE, 'class BasePath/hostpath')
    require(quota.get('enabled') == 'true' and quota.get('data', {}).get('softLimitGrace') == '0%' and quota.get('data', {}).get('hardLimitGrace') == '0%', 'zero-grace quota')

def records_check(r):
    rules = {
        'worker XFS prjquota': r.get('fstype') == 'xfs' and bool(set(r.get('options', [])) & {'prjquota', 'pquota'}),
        'worker user namespaces': type(r.get('userns')) is int and r['userns'] > 0,
        'provisioner ready': r.get('ready') is True,
        'PVC/PV/node binding': r.get('bound') is True,
        'project hard limit': type(r.get('project')) is int and r['project'] > 0 and r.get('hard') == SIZE,
        'UID 1000 user namespace': r.get('uid') == 1000 and r.get('gid') == 1000 and r.get('caps') == 0 and r.get('outside_uid', 0) > 0,
        'fresh project usage': 0 <= r.get('initial_used', -1) < 1024*1024,
        'worker filesystem headroom': r.get('free_bytes', 0) >= SIZE*2 and r.get('free_inodes', 0) > 32,
        'capacity enforced': r.get('write_errno') in (errno.ENOSPC, errno.EDQUOT) and SIZE-1024*1024 <= r.get('written', 0) <= SIZE,
        'project reset rejected': r.get('reset_errno') in (errno.EPERM, errno.EACCES, errno.EINVAL) and r.get('before') == r.get('project') == r.get('after'),
        'probe cleanup': r.get('objects_gone') is True and r.get('row_gone') is True,
    }
    for name, ok in rules.items():
        require(ok, name)

# Runs inside the unprivileged user namespace; a fresh file is owned by UID 1000.
WRITER = r'''
import os,fcntl,struct,json,errno
r={'uid':os.getuid(),'gid':os.getgid(),'caps':int(next(l.split()[1] for l in open('/proc/self/status') if l.startswith('CapEff:')),16),'outside_uid':int(open('/proc/self/uid_map').read().split()[1])}
f=os.open('/data/payload',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
b=bytearray(28);fcntl.ioctl(f,0x801c581f,b,True);r['before']=struct.unpack('=7I',b)[3]
c=bytearray(b);struct.pack_into('=I',c,12,0);r['reset_errno']=0
try:fcntl.ioctl(f,0x401c5820,c)
except OSError as e:r['reset_errno']=e.errno
fcntl.ioctl(f,0x801c581f,b,True);r['after']=struct.unpack('=7I',b)[3]
r['written']=0;r['write_errno']=0
try:
 while r['written']<128*1024*1024:
  r['written']+=os.write(f,b'x'*(1024*1024));os.fsync(f)
except OSError as e:r['write_errno']=e.errno
os.close(f);print(json.dumps(r),flush=True)
'''

def pod(name, spec):
    return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':NAMESPACE},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,**spec}}

def create(args, obj):
    k(args, 'create', '-f', '-', data=json.dumps(obj))

def wait_until(fn, rule, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(2)
    raise Failure(rule)

def rows(text):
    result = {}
    for line in text.splitlines():
        fields = line.strip().lstrip('#').split()
        if fields and fields[0].isdigit():
            require(len(fields) >= 4 and all(x.isdigit() for x in fields[:4]), 'malformed quota row')
            require(int(fields[0]) not in result, 'duplicate quota row')
            result[int(fields[0])] = int(fields[3])*1024
    return result

def verify(args):
    deployment = manifest_check()
    check_class(get(args, 'storageclass', CLASS))
    ns = get(args, 'namespace', NAMESPACE)
    require(ns['metadata'].get('labels', {}).get('pod-security.kubernetes.io/enforce') == 'privileged', 'privileged namespace')
    d = get(args, 'deployment', deployment, '-n', NAMESPACE)
    require(d.get('status', {}).get('observedGeneration', 0) >= d['metadata']['generation'] and d.get('status', {}).get('readyReplicas', 0) == d['spec'].get('replicas', 1) > 0 and d.get('status', {}).get('updatedReplicas', 0) == d['spec'].get('replicas',1), 'provisioner ready')
    name = 'quota-proof-' + uuid.uuid4().hex[:12]
    consumer, observer = name+'-writer', name+'-observer'
    created = []
    pvname = None
    project = None
    record = {'ready':True}
    def delete(kind, n):
        k(args, 'delete', kind, n, '-n', NAMESPACE, '--wait=true', '--timeout=180s')
        created.remove((kind,n))
    def observe(command):
        return k(args, 'exec', '-n', NAMESPACE, observer, '--', 'sh', '-ec', command)
    try:
        create(args, {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':name,'namespace':NAMESPACE},'spec':{'storageClassName':CLASS,'accessModes':['ReadWriteOnce'],'resources':{'requests':{'storage':'64Mi'}}}})
        created.append(('pvc',name))
        create(args,pod(consumer,{'hostUsers':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000,'fsGroup':1000},'containers':[{'name':'writer','image':'python:3.12-slim','command':['sleep','3600'],'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},'volumeMounts':[{'name':'data','mountPath':'/data'}]}],'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':name}}]}))
        created.append(('pod',consumer))
        node=wait_until(lambda: get(args,'pvc',name,'-n',NAMESPACE)['metadata'].get('annotations', {}).get('volume.kubernetes.io/selected-node'), 'probe worker selection (schedulable guest worker required)')
        selected_node=node
        create(args,pod(observer,{'nodeName':node,'containers':[{'name':'observer','image':'openebs/linux-utils:4.6.0','terminationMessagePath':'/tmp/termination-log','command':['sh','-ec','sleep 3600'],'securityContext':{'privileged':True},'volumeMounts':[{'name':'base','mountPath':'/data','readOnly':True},{'name':'dev','mountPath':'/dev','readOnly':True},{'name':'userns','mountPath':'/host-userns','readOnly':True}]}],'volumes':[{'name':'base','hostPath':{'path':BASE,'type':'DirectoryOrCreate'}},{'name':'dev','hostPath':{'path':'/dev','type':'Directory'}},{'name':'userns','hostPath':{'path':'/proc/sys/user/max_user_namespaces','type':'File'}}]}))
        created.append(('pod',observer))
        try:
            k(args,'wait','-n',NAMESPACE,'--for=condition=Ready','pod/'+observer,'--timeout=180s')
        except Failure as e:
            raise Failure('worker prerequisite observer unavailable (BasePath, /dev, user namespace sysctl): '+str(e)) from e
        mount=observe('findmnt -T /data -n -o FSTYPE,OPTIONS').strip().split()
        require(len(mount)==2,'worker mount record')
        record.update(fstype=mount[0],options=mount[1].split(','),userns=int(observe('cat /host-userns').strip()))
        require(record['fstype']=='xfs' and set(record['options']) & {'prjquota','pquota'},'worker XFS prjquota')
        require(record['userns']>0,'worker user namespaces')
        try:
            k(args,'wait','-n',NAMESPACE,'--for=condition=Ready','pod/'+consumer,'--timeout=180s')
        except Failure as e:
            raise Failure('probe provisioning/user namespace idmap mount prerequisite: '+str(e)) from e
        pvc=get(args,'pvc',name,'-n',NAMESPACE); pvname=pvc['spec']['volumeName']; pv=get(args,'pv',pvname); p=get(args,'pod',consumer,'-n',NAMESPACE)
        node=p['spec']['nodeName']; path=pv['spec']['local']['path']
        require(node==selected_node,'PVC selected worker matches writer')
        require(re.fullmatch(re.escape(BASE)+r'/[A-Za-z0-9_-]+',path) is not None,'PV local path under BasePath')
        ref=pv['spec']['claimRef']
        affinity=pv['spec']['nodeAffinity']['required']['nodeSelectorTerms']
        hostname=get(args,'node',node)['metadata']['labels']['kubernetes.io/hostname']
        record['bound']=pvc['status']['phase']=='Bound' and pv['spec']['storageClassName']==CLASS and ref['uid']==pvc['metadata']['uid'] and ref['name']==name and ref['namespace']==NAMESPACE and p['spec']['hostUsers'] is False and any(any(e['key']=='kubernetes.io/hostname' and e['operator']=='In' and e['values']==[hostname] for e in t.get('matchExpressions',[])) for t in affinity)
        require(record['bound'],'PVC/PV/node binding')
        stat=observe('xfs_io -r -c stat /data/'+path.rsplit('/',1)[1])
        ids=re.findall(r'fsxattr.projid\s*=\s*(\d+)',stat)
        require(len(ids)==1 and int(ids[0])>0,'directory project ID')
        project=int(ids[0])
        report=observe('xfs_quota -x -c "report -p -n -N" /data')
        record.update(project=project,hard=rows(report).get(project))
        used=[int(line.strip().lstrip('#').split()[1])*1024 for line in report.splitlines() if line.strip().lstrip('#').split() and line.strip().lstrip('#').split()[0]==str(project)]
        require(len(used)==1 and 0 <= used[0] < 1024*1024, 'fresh project usage')
        record['initial_used']=used[0]
        require(record['fstype']=='xfs' and set(record['options']) & {'prjquota','pquota'},'worker XFS prjquota')
        require(record['userns']>0,'worker user namespaces')
        require(record['hard']==SIZE,'project hard limit')
        free=observe("stat -f -c '%a %S %d' /data").split()
        require(len(free)==3, 'worker filesystem headroom record')
        record.update(free_bytes=int(free[0])*int(free[1]),free_inodes=int(free[2]))
        require(record['free_bytes'] >= SIZE*2 and record['free_inodes'] > 32, 'worker filesystem headroom')
        writer=json.loads(k(args,'exec','-n',NAMESPACE,consumer,'--','python3','-c',WRITER))
        record.update(writer)
        print(f'PROBE pvc={NAMESPACE}/{name} uid={pvc["metadata"]["uid"]} pv={pvname} node={node} path={path} project={project} hard={record["hard"]}')
        delete('pod',consumer); delete('pvc',name)
        wait_until(lambda: all(v['metadata']['name']!=pvname for v in get(args,'pv')['items']),'probe PV deletion')
        wait_until(lambda: project not in rows(observe('xfs_quota -x -c "report -p -n -N" /data')),'quota row cleanup')
        delete('pod',observer)
        record.update(objects_gone=True,row_gone=True)
        records_check(record)
        print('PASS local quota live effects and cleanup')
    finally:
        errors=[]
        for kind,n in sorted(created[:], key=lambda item: 2 if item[1] == observer else (0 if item[0] == 'pod' else 1)):
            try:
                if n == observer and project is not None:
                    try:
                        wait_until(lambda: project not in rows(observe('xfs_quota -x -c "report -p -n -N" /data')), 'quota row cleanup')
                    except Exception as e: errors.append(str(e))
                delete(kind,n)
            except Exception as e: errors.append(str(e))
        if errors: raise Failure('probe cleanup failed: '+'; '.join(errors))

def lifecycle(args):
    manifest_check()
    require(os.environ.get('LOCAL_QUOTA_'+args.command.upper())=='yes',f'set LOCAL_QUOTA_{args.command.upper()}=yes')
    if args.command=='remove':
        for kind in ('pvc','pv'):
            items=get(args,kind,*(['-A'] if kind=='pvc' else []))['items']
            require(not any(i['spec'].get('storageClassName')==CLASS for i in items),'profile still in use: '+kind)
        k(args,'delete','-f',str(ROOT/'demo/ok-storage-local-quota.yaml'),'--ignore-not-found')
        # Keep the namespace: it may hold unrelated resources and must not be deleted.
        docs=[d for d in yaml.safe_load_all((ROOT/'demo/local-quota-provisioner.yaml').read_text()) if d and d['kind']!='Namespace']
        k(args,'delete','-f','-','--ignore-not-found',data=json.dumps({'apiVersion':'v1','kind':'List','items':docs}))
    else:
        k(args,'apply','-f',str(ROOT/'demo/local-quota-provisioner.yaml'))
        k(args,'apply','-f',str(ROOT/'demo/ok-storage-local-quota.yaml'))
    print('PASS local quota '+args.command)

def self_test():
    global ROOT
    good=dict(fstype='xfs',options=['prjquota'],userns=1,ready=True,bound=True,project=12,hard=SIZE,uid=1000,gid=1000,caps=0,outside_uid=65536,write_errno=28,written=SIZE,reset_errno=1,before=12,after=12,objects_gone=True,row_gone=True,free_bytes=SIZE*4,free_inodes=1000,initial_used=0)
    for key,bad,rule in [('fstype','ext4','worker XFS prjquota'),('options',[],'worker XFS prjquota'),('userns',0,'worker user namespaces'),('ready',False,'provisioner ready'),('bound',False,'PVC/PV/node binding'),('hard',SIZE*2,'project hard limit'),('project',0,'project hard limit'),('uid',0,'UID 1000 user namespace'),('gid',0,'UID 1000 user namespace'),('caps',1,'UID 1000 user namespace'),('outside_uid',0,'UID 1000 user namespace'),('initial_used',SIZE,'fresh project usage'),('free_bytes',0,'worker filesystem headroom'),('free_inodes',0,'worker filesystem headroom'),('write_errno',0,'capacity enforced'),('written',SIZE*2,'capacity enforced'),('written',1,'capacity enforced'),('reset_errno',0,'project reset rejected'),('after',0,'project reset rejected'),('objects_gone',False,'probe cleanup'),('row_gone',False,'probe cleanup')]:
        bad_record={**good,key:bad}
        try: records_check(bad_record)
        except Failure as e:
            require(str(e)==rule,'wrong negative failure'); print(f'PASS negative {key}: {e}')
        else: raise Failure('negative accepted: '+key)
    sc=yaml.safe_load((ROOT/'demo/ok-storage-local-quota.yaml').read_text())
    for key,value,rule in [('provisioner','bad','StorageClass provisioner'),('reclaimPolicy','Retain','StorageClass reclaim'),('volumeBindingMode','Immediate','StorageClass binding'),('allowVolumeExpansion',True,'fixed capacity')]:
        mutated=copy.deepcopy(sc);mutated[key]=value
        try:check_class(mutated)
        except Failure as e:
            require(str(e)==rule,'wrong class negative');print(f'PASS negative class {key}: {e}')
        else:raise Failure('class mutation accepted: '+key)
    for label,change,rule in [
        ('name', lambda d: d['metadata'].update(name='other'), 'StorageClass name'),
        ('default', lambda d: d['metadata']['annotations'].update({'storageclass.kubernetes.io/is-default-class':'true'}), 'nondefault class'),
        ('BasePath', lambda d: d['metadata']['annotations'].update({'cas.openebs.io/config':d['metadata']['annotations']['cas.openebs.io/config'].replace(BASE,'/tmp/wrong')}), 'class BasePath/hostpath'),
        ('XFSQuota', lambda d: d['metadata']['annotations'].update({'cas.openebs.io/config':d['metadata']['annotations']['cas.openebs.io/config'].replace('enabled: "true"','enabled: "false"')}), 'zero-grace quota'),
        ('grace', lambda d: d['metadata']['annotations'].update({'cas.openebs.io/config':d['metadata']['annotations']['cas.openebs.io/config'].replace('0%','10%')}), 'zero-grace quota'),
    ]:
        mutated=copy.deepcopy(sc);change(mutated)
        try:check_class(mutated)
        except Failure as e:
            require(str(e)==rule,'wrong class negative');print(f'PASS negative class {label}: {e}')
        else:raise Failure('class mutation accepted: '+label)
    records_check(good)
    records_check({**good, 'reset_errno': errno.EINVAL})
    require(rows('12 64 65536 65536 00 [------]')[12]==SIZE,'quota report units')
    original_root=ROOT
    with tempfile.TemporaryDirectory(prefix='local-quota-negative-') as scratch:
        ROOT=Path(scratch)
        try:
            for folder in ('demo','values'):
                shutil.copytree(original_root/folder,ROOT/folder)
            for filename in ('demo/local-quota-chart-4.6.0.tgz','demo/local-quota-provisioner.yaml','demo/ok-storage-local-quota.yaml','values/local-quota-values.yaml'):
                target=ROOT/filename;original=target.read_bytes();target.write_bytes(original+b'corrupt')
                try:manifest_check()
                except Failure as e:print(f'PASS negative pin {filename}: {e}')
                else:raise Failure('corrupted pinned file accepted')
                target.write_bytes(original)
        finally: ROOT=original_root
    manifest_check()
    print('PASS local quota positive records and pinned offline manifests')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['apply','verify','remove','self-test','manifest-check']);parser.add_argument('--kubeconfig');args=parser.parse_args()
    if args.command=='self-test':return self_test()
    if args.command=='manifest-check':manifest_check();print('PASS local quota pinned manifests');return
    require(bool(args.kubeconfig and Path(args.kubeconfig).is_file()),'explicit guest --kubeconfig file required')
    if args.command=='verify':verify(args)
    else:lifecycle(args)

if __name__=='__main__':
    try: main()
    except (Failure, OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired) as e:
        print('FAIL: '+str(e),file=sys.stderr);sys.exit(2)
