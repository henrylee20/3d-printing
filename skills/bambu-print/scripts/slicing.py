"""Installed Bambu profiles -> P1S slicing -> integrity/support report -> cloud ZIP."""
from pathlib import Path
import hashlib,json,math,os,re,shutil,subprocess,xml.etree.ElementTree as ET,zipfile
from thumbnail import preview_png

BED_NAMES={'textured':'Textured PEI Plate','smooth':'High Temp Plate'}
BED_CODES={'Textured PEI Plate':'textured_plate','High Temp Plate':'hot_plate'}

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,obj):Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')

def resolve_profile(root,kind,name,seen=()):
    if name in seen:raise ValueError('预设继承存在循环')
    data=json.loads((root/kind/(name+'.json')).read_text())
    out=resolve_profile(root,kind,data['inherits'],seen+(name,)) if data.get('inherits') else {}
    out.update(data)
    for included in data.get('include',[]):
        out.update({k:v for k,v in resolve_profile(root,kind,included,seen+(name,)).items() if k not in ['name','from','instantiation','type','inherits','include']})
    out.pop('include',None)
    return out

def runtime(studio,cache):
    """The official CLI resolves resources relative to the real executable path."""
    exe=studio/'bin/bambu-studio';res=studio/'resources'
    required=['machine_full/Bambu Lab P1S.json','machine_full/Bambu Lab P1S 0.4 nozzle.json','process_full/0.20mm Standard @BBL X1C.json']
    if all((res/'profiles/BBL'/name).exists() for name in required):return exe
    dest=cache/'studio-runtime';target=dest/'bin/bambu-studio'
    if not target.exists() or target.stat().st_size!=exe.stat().st_size or target.stat().st_mtime_ns!=exe.stat().st_mtime_ns:
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(exe,target)
    # Supply CLI profile files in a resource overlay; preserve the installation.
    def overlay(src,dst,branches):
        dst.mkdir(parents=True,exist_ok=True)
        for child in src.iterdir():
            if child.name in branches:continue
            out=dst/child.name
            if not out.exists() and not out.is_symlink():out.symlink_to(child,target_is_directory=child.is_dir())
    overlay(res,dest/'resources',{'profiles'})
    overlay(res/'profiles',dest/'resources/profiles',{'BBL'})
    overlay(res/'profiles/BBL',dest/'resources/profiles/BBL',{'machine_full','process_full'})
    full=dest/'resources/profiles/BBL/machine_full';full.mkdir(exist_ok=True)
    shutil.copy2(res/'profiles/BBL/machine/Bambu Lab P1S.json',full/'Bambu Lab P1S.json')
    for kind,name in [('machine','Bambu Lab P1S 0.4 nozzle'),('process','0.20mm Standard @BBL X1C')]:
        folder=dest/'resources/profiles/BBL'/(kind+'_full');folder.mkdir(exist_ok=True)
        profile=resolve_profile(res/'profiles/BBL',kind,name);profile.pop('inherits',None)
        save(folder/(name+'.json'),profile)
    return target

def read_archive(path):
    with zipfile.ZipFile(path) as z:
        if z.testzip():raise ValueError('3MF ZIP 完整性校验失败')
        files={n:z.read(n) for n in z.namelist()}
    # CLI 02.08.02.60 emits raw quotes in string-list object metadata after
    # placement exports. Escape the attributes while preserving their values.
    key='Metadata/model_settings.config'
    if key in files:
        files[key]=re.sub(rb'(<metadata key="[^"]+" value=")([^<\r\n]*)("/>)',
            lambda m:m[1]+m[2].replace(b'"',b'&quot;')+m[3],files[key])
    return files

def check_archive(path):
    files=read_archive(path)
    gcodes=[n for n in files if n.endswith('.gcode')]
    if gcodes!=['Metadata/plate_1.gcode']:raise ValueError('仅支持第 1 盘、单盘已切片 3MF')
    code=files[gcodes[0]]
    if hashlib.md5(code).hexdigest().upper()!=files[gcodes[0]+'.md5'].decode().strip().upper():raise ValueError('G-code MD5 不匹配')
    cfg=json.loads(files['Metadata/project_settings.config']);meta=ET.fromstring(files['Metadata/slice_info.config'])
    plates=meta.findall('plate')
    if len(plates)!=1:raise ValueError('仅支持单盘任务')
    items={m.attrib['key']:m for m in plates[0].findall('metadata')}
    values={k:m.attrib['value'] for k,m in items.items()}
    if values.get('printer_model_id')!='C12' or cfg.get('printer_model')!='Bambu Lab P1S':raise ValueError('切片机型必须为 P1S')
    if cfg.get('nozzle_diameter')!=['0.4']:raise ValueError('仅支持 0.4 mm 喷嘴')
    filaments=plates[0].findall('filament')
    if len(filaments)!=1 or filaments[0].attrib['type']!='PLA':raise ValueError('仅支持单耗材外置 PLA')
    if values.get('outside')!='false':raise ValueError('切片超出打印区域或未提供区域校验')
    if cfg.get('curr_bed_type') not in BED_CODES:raise ValueError('不支持的打印板类型')
    if values.get('pause_count','0')!='0':raise ValueError('任务含暂停点，请先在 Studio 检查')
    seconds=round(float(values['prediction']));first=float(values['first_layer_time'])
    if not math.isfinite(first) or first<0 or first>seconds:
        items['first_layer_time'].set('value','0.000000')
        files['Metadata/slice_info.config']=ET.tostring(meta,encoding='utf-8',xml_declaration=True)
    text=code.decode();features=set(re.findall(r'^; FEATURE: (.+)$',text,re.M))
    return files,{'bed_type':BED_CODES[cfg['curr_bed_type']], 'estimated_seconds':seconds,
        'estimated_grams':round(sum(float(f.attrib['used_g']) for f in filaments),2),
        'layer_height':cfg['layer_height'],'ironing':cfg.get('ironing_type','no ironing'),
        'support_enabled':cfg.get('enable_support')=='1','support_paths_present':any('Support' in s for s in features),
        'features':sorted(features),'gcode_md5_verified':True}

def zip_write(path,files):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for n,b in files.items():z.writestr(n,b)

def cli_warnings(directory):
    """Read official warnings, including those overwritten in result.json.

    BambuStudio.cpp keeps only the last warning in each plate's JSON field;
    the CLI warning log contains every warning. LC_ALL=C fixes their language.
    """
    result=json.loads((directory/'result.json').read_text())
    plates=result.get('sliced_plates',[])
    if result.get('return_code')!=0 or len(plates)!=1 or plates[0].get('id')!=1:
        raise ValueError('Bambu CLI 未完成单盘切片检查；查看 slice.log / result.json')
    if not isinstance(plates[0].get('warning_message'),str):
        raise ValueError('Bambu CLI 结果缺少 warning_message，无法确认检查结果')
    messages=[plates[0]['warning_message']]
    log=(directory/'slice.log').read_text(errors='replace')
    messages+=re.findall(r'plate \d+: found (?:NON_CRITICAL )?slicing warnings: ([^\r\n]+)',log)
    messages=[re.sub(r', no_check=\d+\s*$','',m).strip() for m in messages]
    warnings=list(dict.fromkeys(m for m in messages if m))
    # PrintObject.cpp / SlicingNeedSupportOn: these are Bambu's three reasons.
    risk=[m for m in warnings if re.search(r'has (floating regions|floating cantilever|large overhangs)\. Please re-orient the object or enable support generation\.',m)]
    return {'warnings':warnings,'support_warnings':risk}


def blocking_reasons(preflight,final,actual):
    reasons=[]
    if any(m not in preflight['support_warnings'] for m in preflight['warnings']):
        reasons.append('Bambu CLI 预切片有其他警告；查看检查报告')
    if final['warnings']:reasons.append('Bambu CLI 最终切片仍有警告；查看检查报告')
    if preflight['support_warnings']:
        if not actual['support_enabled']:reasons.append('Bambu CLI 提示需要支撑，但支撑被关闭')
        elif not actual['support_paths_present']:reasons.append('Bambu CLI 提示需要支撑，但未生成支撑路径')
    return reasons


def validate_support(directory,report,actual):
    if report.get('source')!='bambu_cli':
        raise ValueError('缺少 Bambu CLI 悬空检查；请重新 prepare 源模型')
    final_name=report.get('final_pass')
    expected=['check-no-support']+(['check-with-support'] if final_name=='check-with-support' else [])
    if final_name not in expected or set(report.get('passes',{}))!=set(expected):
        raise ValueError('Bambu CLI 检查记录不完整')
    checks={}
    for name in expected:
        record=report['passes'][name]
        for file in ['result.json','slice.log']:
            if digest(directory/name/file)!=record['sha256'][file]:
                raise ValueError('Bambu CLI 检查记录已变更；请重新准备')
        checks[name]=cli_warnings(directory/name)
    if actual['support_enabled']!=(final_name=='check-with-support'):
        raise ValueError('最终支撑设置与 Bambu CLI 检查记录不一致')
    reasons=blocking_reasons(checks['check-no-support'],checks[final_name],actual)
    if reasons:raise ValueError('；'.join(reasons))


def prepare(source,out,args,studio,cache):
    source=Path(source).resolve();out=out.resolve()
    if out.exists() and any(out.iterdir()):raise ValueError('产物目录必须为空；已有任务可直接用 print 目录')
    out.mkdir(parents=True,exist_ok=True)
    is_stl=source.suffix.lower()=='.stl'
    if not is_stl and source.suffix.lower()!='.3mf':raise ValueError('输入必须为 STL 或含模型的已切片单盘 3MF')
    if not is_stl and any(getattr(args,k,None) is not None for k in ['ironing','layer','infill','walls','bed','support_angle']):
        raise ValueError('修改 3MF 的其他工艺请提供 STL；3MF 保留原工艺并重新检查、切片')
    support=args.support or 'auto';input_files=None
    job=out/'job.gcode.3mf'
    if is_stl:
        profiles=studio/'resources/profiles/BBL'
        machine=resolve_profile(profiles,'machine','Bambu Lab P1S 0.4 nozzle')
        process=resolve_profile(profiles,'process','0.20mm Standard @BBL X1C')
        filament=resolve_profile(profiles,'filament',args.filament_preset)
        machine['printer_settings_id']=machine['name']
        process.update(name='P1S skill process',print_settings_id='P1S skill process',compatible_printers=[machine['name']])
        filament['filament_settings_id']=[filament['name']]
        layer=args.layer or .2
        process.update(layer_height=str(layer),ironing_type={'off':'no ironing','topmost':'topmost','top':'top'}[args.ironing or 'off'],
            enable_support='0',support_type='normal(auto)',support_threshold_angle=str(round(args.support_angle or 45)),
            support_on_build_plate_only='0',enable_prime_tower='0',detect_floating_vertical_shell='1')
        if args.infill is not None:
            process['sparse_infill_density']=str(args.infill)+'%'
            if args.infill==100:process['sparse_infill_pattern']='zig-zag'
        if args.walls is not None:process['wall_loops']=str(args.walls)
        for kind,d in [('machine',machine),('process',process),('filament',filament)]:
            d.pop('inherits',None);d['instantiation']='true'
            if kind=='process':d.update(**{'from':'User','inherits':'0.20mm Standard @BBL X1C'})
            save(out/(kind+'.json'),d)
    else:
        input_files,_=check_archive(source)
        if not any(n.startswith('3D/') and n.endswith('.model') for n in input_files):
            raise ValueError('3MF 不含模型，无法执行 Bambu CLI 检查；请提供源 STL')
        process=json.loads(input_files['Metadata/project_settings.config'])
        # Explicitly loading the same printer preset makes the headless CLI retain
        # its model ID in exported slice_info.config. Preserve embedded values.
        machine=resolve_profile(studio/'resources/profiles/BBL','machine','Bambu Lab P1S 0.4 nozzle')
        machine.update({k:process[k] for k in machine if k in process and k not in ['name','type','from','inherits','instantiation','version']})
        machine.pop('inherits',None);machine['instantiation']='true'
        save(out/'machine.json',machine)
        # An explicit filament preset prevents CLI upload mode from resetting
        # prime volume to 45. Populate it with this project's embedded values.
        filament=resolve_profile(studio/'resources/profiles/BBL','filament','Bambu PLA Basic @BBL P1S 0.4 nozzle')
        filament.update({k:v for k,v in process.items() if (k in filament or k.startswith('filament_')) and k not in ['name','type','from','inherits','instantiation','version']})
        filament['name']=process['filament_settings_id'][0]
        filament.pop('inherits',None);filament['instantiation']='true'
        save(out/'filament.json',filament)

    env=os.environ.copy();env['LD_LIBRARY_PATH']=str(studio/'bin');env['LC_ALL']='C'
    executable=runtime(studio,cache);passes={}
    auto_place=args.auto_place is not False
    placed=out/'placed.3mf'
    command=[str(executable),'--datadir',str(out/'placement_data'),'--debug','2','--check-preset',
        '--load-settings',str(out/'machine.json')+(';' + str(out/'process.json') if is_stl else ''),
        '--load-filaments',str(out/'filament.json')]
    if is_stl:command+=['--curr-bed-type',BED_NAMES[args.bed or 'textured']]
    command+=['--orient','1' if auto_place else '0','--arrange','1' if auto_place else '0','--ensure-on-bed',
        '--export-3mf',placed.name,'--outputdir',str(out),str(source)]
    with (out/'placement.log').open('w') as log:
        result=subprocess.run(command,cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=600)
    if result.returncode:raise ValueError(f'Bambu CLI 摆放失败 ({result.returncode})；查看 {out}/placement.log')
    placed_files=read_archive(placed)
    if '3D/3dmodel.model' not in placed_files:raise ValueError('Bambu CLI 未导出摆放模型')
    ET.fromstring(placed_files['Metadata/model_settings.config'])
    zip_write(placed,placed_files)
    placed_config=json.loads(placed_files['Metadata/project_settings.config'])
    placement={'mode':'auto' if auto_place else 'preserve','file':placed.name,'sha256':digest(placed)}
    def run_pass(enabled):
        name='check-with-support' if enabled else 'check-no-support'
        directory=out/name;directory.mkdir()
        pass_process={**placed_config,'enable_support':'1' if enabled else '0'}
        # Forced layers also make has_support() true and skip Bambu's check.
        if not enabled:pass_process['enforce_support_layers']='0'
        command=[str(executable),'--datadir',str(directory/'studio_data'),'--debug','2','--check-preset',
            '--load-settings',str(out/'machine.json'),'--load-filaments',str(out/'filament.json')]
        # Both passes share the placed model; only support settings change.
        files=dict(placed_files);files['Metadata/project_settings.config']=json.dumps(pass_process).encode()
        settings=ET.fromstring(files['Metadata/model_settings.config'])
        for m in settings.iter('metadata'):
            if m.attrib.get('key')=='enable_support':m.set('value',pass_process['enable_support'])
            if not enabled and m.attrib.get('key')=='enforce_support_layers':m.set('value','0')
        files['Metadata/model_settings.config']=ET.tostring(settings,encoding='utf-8',xml_declaration=True)
        model=directory/'input.3mf';zip_write(model,files)
        command+=['--orient','0','--arrange','0','--ensure-on-bed',
            '--slice','0','--export-3mf',job.name,'--outputdir',str(directory),str(model)]
        with (directory/'slice.log').open('w') as log:
            result=subprocess.run(command,cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=600)
        if result.returncode:raise ValueError(f'Bambu CLI 切片失败 ({result.returncode})；查看 {directory}/slice.log')
        warnings=cli_warnings(directory)
        _,actual=check_archive(directory/job.name)
        if actual['support_enabled']!=enabled:raise ValueError('Bambu CLI 未采用指定的支撑设置')
        passes[name]={**warnings,'support_enabled':enabled,'support_paths_present':actual['support_paths_present'],
            'placement_sha256':placement['sha256'],
            'sha256':{n:digest(directory/n) for n in ['result.json','slice.log']}}
        return name

    # Bambu only runs its support-necessity check with support disabled.
    final=run_pass(False)
    recommended=bool(passes[final]['support_warnings'])
    if support=='on' or (support=='auto' and recommended):final=run_pass(True)
    screen={'source':'bambu_cli','support_recommended':recommended,'final_pass':final,'passes':passes}
    save(out/'support_report.json',screen)
    for filename in [job.name,'result.json','slice.log']:shutil.copyfile(out/final/filename,out/filename)
    files,report=check_archive(job);original=files['Metadata/plate_1.gcode']
    # Headless CLI omits preview images although its package relationships name them.
    for name,size in [('plate_1.png',512),('plate_1_small.png',128)]:
        files['Metadata/'+name]=preview_png(job,size)
    # Match official config export: omit executable G-code and its relationships.
    zip_write(job,files)
    config={n:b for n,b in files.items() if not n.endswith(('.gcode','.gcode.md5'))}
    config.pop('Metadata/_rels/model_settings.config.rels',None)
    model_xml=ET.fromstring(config['Metadata/model_settings.config'])
    for plate in model_xml.findall('plate'):
        for m in list(plate):
            if m.tag=='metadata' and m.attrib.get('key')=='gcode_file':plate.remove(m)
    config['Metadata/model_settings.config']=ET.tostring(model_xml,encoding='utf-8',xml_declaration=True)
    zip_write(out/'cloud_config.3mf',config)
    with zipfile.ZipFile(job) as z:
        if z.read('Metadata/plate_1.gcode')!=original:raise ValueError('封装时 G-code 发生变化')
    blocked=blocking_reasons(passes['check-no-support'],passes[final],report)
    report.update(source=str(source),source_sha256=digest(source),support_report=screen,placement=placement,
        support_mode=support,slicer_support_decision='generated' if report['support_paths_present'] else 'none generated',slicer_warnings=passes[final]['warnings'],blocked_reasons=blocked,job_sha256=digest(job),
        config_sha256=digest(out/'cloud_config.3mf'),thumbnails_available='Metadata/plate_1.png' in files,source_type='stl' if is_stl else 'resliced_3mf',printer_model='P1S',nozzle_mm=.4,material='PLA',feed_source='external')
    save(out/'job.json',report)
    return report
