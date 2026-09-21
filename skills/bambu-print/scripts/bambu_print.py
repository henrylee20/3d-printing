#!/usr/bin/env python3
"""P1S / 0.4 mm / external PLA: prepare, query status, or dispatch once."""
from pathlib import Path
import argparse,fcntl,hashlib,json,os,re,shutil,subprocess,sys,tempfile,threading,uuid,zipfile

ROOT=Path(__file__).resolve().parent

def parser():
    p=argparse.ArgumentParser(description=__doc__,epilog='依赖：Python 3、NumPy、g++、已登录的 Bambu Studio；桥接 ABI 已验证为官方网络插件 02.08.02.54。')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ['status','prepare','print']:
        q=sub.add_parser(name,help={'status':'查询设备状态，不打印','prepare':'检查悬空并切片/封装，不打印','print':'检查设备并启动一次打印'}[name])
        q.add_argument('--printer',help='云端设备名称或序列号；默认 Studio 当前选择的设备')
        q.add_argument('--studio',type=Path,default=Path(os.environ.get('BAMBU_STUDIO_HOME','/opt/bambustudio-bin')),help='Bambu Studio 安装目录')
        q.add_argument('--account-dir',type=Path,default=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'BambuStudio')
        q.add_argument('--cache-dir',type=Path,default=Path.home()/'workspace/.bambu-print-cache')
        q.add_argument('--country',choices=['CN','US'],help='默认从 Studio 账号区域推断')
        if name=='status':continue
        q.add_argument('input',type=Path,help='STL、含模型的已切片单盘 3MF（重新切片）；print 也接受已准备的任务目录')
        q.add_argument('--out',type=Path,help='新的空产物目录；默认 <文件名>.print')
        q.add_argument('--support',choices=['auto','on','off'],help='默认 auto：按 Bambu CLI 无支撑预切片警告决定；on/off 也先检查')
        q.add_argument('--support-angle',type=float,help='STL 的 Bambu 支撑生成阈值角，默认 45°；不改变官方风险检查规则')
        q.add_argument('--ironing',choices=['off','topmost','top'],help='STL 熨烫；默认 off')
        q.add_argument('--layer',type=float,help='STL 层高 0.08–0.28 mm，默认 0.20')
        q.add_argument('--infill',type=int,help='STL 填充率 0–100，默认官方工艺')
        q.add_argument('--walls',type=int,help='STL 墙数，默认官方工艺')
        q.add_argument('--bed',choices=['textured','smooth'],help='STL 打印板，默认 textured')
        q.add_argument('--filament-preset',default='Bambu PLA Basic @BBL P1S 0.4 nozzle',help='已安装的 PLA 耗材预设名称')
        if name=='print':
            q.add_argument('--dry-run',action='store_true',help='只准备并验证文件；不连接云端、不下发')
            q.add_argument('--name',help='任务名称前缀')
            q.add_argument('--bed-leveling',action=argparse.BooleanOptionalAction,default=True)
            q.add_argument('--timelapse',action=argparse.BooleanOptionalAction,default=False)
    return p

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def dump(obj):print(json.dumps(obj,ensure_ascii=False),flush=True)

def validate_job(directory):
    from slicing import check_archive,validate_support
    d=json.loads((directory/'job.json').read_text())
    for filename,key in [('job.gcode.3mf','job_sha256'),('cloud_config.3mf','config_sha256')]:
        if sha(directory/filename)!=d[key]:raise ValueError(f'{filename} 已变更；请重新准备')
    _,actual=check_archive(directory/'job.gcode.3mf')
    if d.get('blocked_reasons'):raise ValueError('；'.join(d['blocked_reasons']))
    validate_support(directory,d.get('support_report',{}),actual)
    if (directory/'dispatch_attempt.json').exists():raise ValueError('此目录已经尝试下发；先查打印机和 receipt.json，避免重复打印')
    return d

def compile_bridge(cache):
    sources=[ROOT/'cloud_bridge.cpp',ROOT/'vendor/bambu_networking.hpp',ROOT/'vendor/json.hpp']
    key=hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()[:16]
    binary=cache/('cloud-bridge-'+key)
    if not binary.exists():
        temporary=cache/('build-'+uuid.uuid4().hex)
        result=subprocess.run(['g++','-std=c++17','-O2',str(sources[0]),'-o',str(temporary),'-ldl','-pthread'],capture_output=True,text=True,timeout=90)
        if result.returncode:raise ValueError('桥接程序编译失败：'+result.stderr[-3000:])
        temporary.replace(binary)
    return binary

def cloud(args,directory=None,job=None):
    account=args.account_dir.resolve();app=json.loads((account/'BambuStudio.conf').read_text())['app']
    printer=args.printer or app.get('user_last_selected_machine')
    if not printer:raise ValueError('请用 --printer 指定设备名称或序列号')
    region=app.get('region','')
    country=args.country or ('CN' if region=='China' else 'US')
    system_ca=Path('/etc/ssl/certs/ca-certificates.crt')
    if not system_ca.is_file():raise ValueError('系统 CA 证书库不存在')
    plugin=account/'plugins/libbambu_networking.so'
    if not plugin.is_file():raise ValueError('请先在 Bambu Studio 登录并安装官方网络插件')
    cache=args.cache_dir.resolve();cache.mkdir(parents=True,exist_ok=True)
    binary=compile_bridge(cache)
    opts={'printer':printer,'country_code':country,'plugin_path':str(plugin),
        'cert_dir':str(args.studio.resolve()/'resources/cert'),'slicer_uuid':app.get('slicer_uuid','')}
    if directory:
        stem=args.name or directory.name
        name=re.sub(r'[^a-zA-Z0-9_-]','_',stem)[:60].strip('_') or 'Bambu_print'
        opts.update(filename=str(directory/'job.gcode.3mf'),config_filename=str(directory/'cloud_config.3mf'),
            task_name=name+'_'+uuid.uuid4().hex[:8],bed_type=job['bed_type'],bed_leveling=args.bed_leveling,
            timelapse=args.timelapse,attempt_marker=str(directory/'dispatch_attempt.json'),receipt_file=str(directory/'receipt.json'))
    events=[];last_progress=None;timed_out=threading.Event()
    # Serialize preflight + dispatch across callers using this cache.
    with (cache/'cloud.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if directory:validate_job(directory)
        with tempfile.TemporaryDirectory(prefix='session-',dir=cache) as tmp:
            session=Path(tmp);session.chmod(0o700)
            shutil.copyfile(account/'BambuNetworkEngine.conf',session/'BambuNetworkEngine.conf')
            (session/'BambuNetworkEngine.conf').chmod(0o600)
            (session/'options.json').write_text(json.dumps(opts));(session/'options.json').chmod(0o600)
            env=os.environ.copy();env['SSL_CERT_FILE']=str(system_ca)
            with (session/'diagnostics.log').open('w') as log:
                child=subprocess.Popen([str(binary),str(session),'print' if directory else 'status'],env=env,stdout=subprocess.PIPE,stderr=log,text=True)
                def expire():timed_out.set();child.kill()
                timer=threading.Timer(240,expire);timer.daemon=True;timer.start()
                try:
                    for line in child.stdout:
                        if not line.startswith('EVENT '):continue
                        event=json.loads(line[6:]);events.append(event)
                        if 'dispatch_stage' in event:
                            progress=(event['dispatch_stage'],event['stage_code']//10)
                            if progress==last_progress:continue
                            last_progress=progress
                        # Full telemetry goes only into the local event report.
                        if 'printer' not in event:dump(event)
                    rc=child.wait()
                except BaseException:
                    child.kill();child.wait();raise
                finally:timer.cancel()
    report=directory/'events.json' if directory else cache/'last_status.json'
    report.write_text(json.dumps(events,ensure_ascii=False,indent=2)+'\n')
    if timed_out.is_set():raise ValueError('云端调用超时；状态不确定，先查询设备。下发标记已保留')
    if rc:raise ValueError(f'网络插件调用未确认成功（退出码 {rc}），详情：{report}')
    if directory:
        receipt=json.loads((directory/'receipt.json').read_text())
        if receipt.get('printer_start_confirmed') is not True:raise ValueError('文件已发送，但设备尚未确认启动')
        dump({'printer_start_confirmed':True,'estimated_seconds':job['estimated_seconds'],
              'estimated_grams':job['estimated_grams'],'receipt':str(directory/'receipt.json')})

def main(argv=None):
    args=parser().parse_args(argv)
    if args.command=='status':cloud(args);return
    if args.support_angle is not None and not 0<args.support_angle<90:raise ValueError('支撑角度必须在 0–90° 之间')
    if args.layer is not None and not .08<=args.layer<=.28:raise ValueError('层高必须在 0.08–0.28 mm 之间')
    if args.infill is not None and not 0<=args.infill<=100:raise ValueError('填充率必须在 0–100 之间')
    if args.walls is not None and not 1<=args.walls<=20:raise ValueError('墙数必须在 1–20 之间')
    from slicing import prepare
    if args.input.is_dir():
        if args.command!='print':raise ValueError('prepare 需要模型文件')
        if any(getattr(args,k,None) is not None for k in ['out','support','support_angle','ironing','layer','infill','walls','bed']):
            raise ValueError('已准备任务的参数来自 job.json；变更工艺请重新 prepare 源模型')
        directory=args.input.resolve();job=validate_job(directory)
    else:
        directory=(args.out or Path.cwd()/(args.input.stem+'.print')).resolve()
        args.cache_dir.mkdir(parents=True,exist_ok=True)
        job=prepare(args.input,directory,args,args.studio.resolve(),args.cache_dir.resolve())
    dump({'job_directory':str(directory),'estimated_seconds':job['estimated_seconds'],
          'estimated_grams':job['estimated_grams'],'support_recommended':job['support_report']['support_recommended'],
          'support_enabled':job['support_enabled'],'blocked_reasons':job['blocked_reasons']})
    if args.command=='prepare':return
    validate_job(directory)
    if args.dry_run:dump({'dry_run':True,'cloud_contacted':False,'printer_job_sent':False});return
    cloud(args,directory,job)

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,subprocess.SubprocessError,zipfile.BadZipFile) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);sys.exit(1)
