#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LeRobot 远程训练平台
支持数据集上传、模型训练、模型下载
"""

import os
import time
import subprocess
import threading
import shutil
import re
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename

app = Flask(__name__)

# 训练进程管理
training_processes = {}
training_logs = {}
training_configs = {}

# 目录配置
UPLOAD_FOLDER = './gpufree-data/upload_temp'
DOWNLOAD_FOLDER = './gpufree-data/model_output'
DOWNLOAD_TEMP_FOLDER = './gpufree-data/download_temp'
MAX_CONTENT_LENGTH = 10 * 1024 * 1024 * 1024

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH


def ensure_directories():
    """确保目录存在"""
    for folder in [UPLOAD_FOLDER, DOWNLOAD_FOLDER, DOWNLOAD_TEMP_FOLDER]:
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)


@app.route('/list_datasets', methods=['GET'])
def list_datasets():
    """列出数据集"""
    ensure_directories()
    try:
        datasets = []
        abs_path = os.path.abspath(UPLOAD_FOLDER)
        if os.path.exists(abs_path):
            for name in os.listdir(abs_path):
                folder = os.path.join(abs_path, name)
                if os.path.isdir(folder):
                    is_valid = all(os.path.isdir(os.path.join(folder, d)) for d in ['meta', 'data', 'videos'])
                    datasets.append({
                        'name': name, 'path': folder, 'is_valid': is_valid,
                        'modified_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(folder))),
                        'timestamp': os.path.getmtime(folder)
                    })
        datasets.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify({'success': True, 'datasets': datasets, 'total': len(datasets)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def extract_output_dir(command):
    match = re.search(r'--output_dir[=\s]+([^\s\\]+)', command)
    return match.group(1) if match else None


def pack_model(output_dir, task_id):
    ensure_directories()
    if not output_dir or not os.path.exists(output_dir):
        return None, f"目录不存在: {output_dir}"
    try:
        folder_name = os.path.basename(output_dir.rstrip('/'))
        zip_name = f"{folder_name}_{time.strftime('%Y%m%d_%H%M%S')}"
        zip_path = os.path.join(DOWNLOAD_TEMP_FOLDER, zip_name)
        shutil.make_archive(zip_path, 'zip', output_dir)
        return f"{zip_name}.zip", None
    except Exception as e:
        return None, str(e)


@app.route('/start_training', methods=['POST'])
def start_training():
    """开始训练"""
    global training_processes, training_logs, training_configs
    try:
        data = request.get_json()
        if not data or 'command' not in data:
            return jsonify({'error': '缺少训练命令'}), 400
        
        command = data['command']
        task_id = data.get('task_id', f"train_{int(time.time())}")
        shutdown_after = data.get('shutdown_after', False)
        output_dir = extract_output_dir(command)
        
        if task_id in training_processes and training_processes[task_id].poll() is None:
            training_processes[task_id].terminate()
        
        training_configs[task_id] = {'output_dir': output_dir, 'shutdown_after': shutdown_after}
        training_logs[task_id] = []
        
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, env={**os.environ, 'PYTHONUNBUFFERED': '1'})
        training_processes[task_id] = process
        
        def collect_logs():
            try:
                for line in iter(process.stdout.readline, ''):
                    if line:
                        training_logs[task_id].append({'time': time.strftime('%H:%M:%S'), 'message': line.rstrip()})
                        if len(training_logs[task_id]) > 1000:
                            training_logs[task_id] = training_logs[task_id][-1000:]
                process.wait()
                config = training_configs.get(task_id, {})
                if process.returncode == 0 and config.get('output_dir'):
                    training_logs[task_id].append({'time': time.strftime('%H:%M:%S'), 'message': '[系统] 开始打包模型...'})
                    zip_name, err = pack_model(config['output_dir'], task_id)
                    msg = f'[系统] 打包完成: {zip_name}' if zip_name else f'[系统] 打包失败: {err}'
                    training_logs[task_id].append({'time': time.strftime('%H:%M:%S'), 'message': msg})
                    if config.get('shutdown_after'):
                        training_logs[task_id].append({'time': time.strftime('%H:%M:%S'), 'message': '[系统] 即将关机...'})
                        time.sleep(3)
                        os.system('shutdown -h now')
            except Exception as e:
                training_logs[task_id].append({'time': time.strftime('%H:%M:%S'), 'message': f'[错误] {e}'})
        
        threading.Thread(target=collect_logs, daemon=True).start()
        return jsonify({'success': True, 'task_id': task_id, 'pid': process.pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/training_status', methods=['GET'])
def training_status():
    """获取训练状态"""
    task_id = request.args.get('task_id')
    last_index = int(request.args.get('last_index', 0))
    if not task_id:
        return jsonify({'error': '缺少task_id'}), 400
    
    is_running = task_id in training_processes and training_processes[task_id].poll() is None
    exit_code = None if is_running else (training_processes[task_id].returncode if task_id in training_processes else None)
    logs = training_logs.get(task_id, [])
    
    return jsonify({
        'success': True, 'task_id': task_id, 'is_running': is_running,
        'exit_code': exit_code, 'logs': logs[last_index:], 'last_index': len(logs)
    })


@app.route('/stop_training', methods=['POST'])
def stop_training():
    """停止训练"""
    try:
        task_id = request.get_json().get('task_id')
        if not task_id or task_id not in training_processes:
            return jsonify({'error': '任务不存在'}), 404
        process = training_processes[task_id]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except:
                process.kill()
            training_logs[task_id].append({'time': time.strftime('%H:%M:%S'), 'message': '[系统] 训练已停止'})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/list_models', methods=['GET'])
def list_models():
    """列出模型"""
    ensure_directories()
    try:
        models = []
        abs_path = os.path.abspath(DOWNLOAD_TEMP_FOLDER)
        if os.path.exists(abs_path):
            for name in os.listdir(abs_path):
                fp = os.path.join(abs_path, name)
                if os.path.isfile(fp) and name.endswith('.zip'):
                    size = os.path.getsize(fp)
                    models.append({
                        'name': name, 'size_mb': f"{size/(1024*1024):.2f} MB",
                        'modified_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(fp))),
                        'timestamp': os.path.getmtime(fp)
                    })
        models.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify({'success': True, 'models': models})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/download_model', methods=['GET'])
def download_model():
    """下载模型"""
    filename = request.args.get('filename')
    if not filename:
        return jsonify({'error': '缺少文件名'}), 400
    fp = os.path.join(DOWNLOAD_TEMP_FOLDER, secure_filename(filename))
    if not os.path.exists(fp):
        return jsonify({'error': '文件不存在'}), 404
    return send_file(fp, as_attachment=True, download_name=filename, mimetype='application/zip')


@app.route('/upload_folder', methods=['POST'])
def upload_folder():
    """上传文件夹"""
    ensure_directories()
    if 'files' not in request.files:
        return jsonify({'error': '没有文件'}), 400
    
    files = request.files.getlist('files')
    paths = request.form.getlist('paths')
    if not files:
        return jsonify({'error': '没有选择文件'}), 400
    
    original_root = paths[0].split('/')[0] if paths and '/' in paths[0] else ''
    root_folder = f"{original_root}_{time.strftime('%Y%m%d_%H%M%S')}" if original_root else f"upload_{time.strftime('%Y%m%d_%H%M%S')}"
    
    total_size, uploaded = 0, []
    start = time.time()
    
    for i, file in enumerate(files):
        if file.filename == '':
            continue
        try:
            rel_path = paths[i] if i < len(paths) else file.filename
            if original_root and rel_path.startswith(original_root + '/'):
                rel_path = root_folder + rel_path[len(original_root):]
            target = os.path.join(UPLOAD_FOLDER, rel_path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            file.save(target)
            size = os.path.getsize(target)
            total_size += size
            uploaded.append({'path': rel_path, 'size': size})
        except:
            pass
    
    duration = max(time.time() - start, 0.001)
    size_mb = total_size / (1024 * 1024)
    
    return jsonify({
        'success': True, 'root_folder': root_folder,
        'upload_path': os.path.abspath(os.path.join(UPLOAD_FOLDER, root_folder)),
        'total_files': len(uploaded), 'total_size': f"{size_mb:.2f} MB",
        'speed': f"{size_mb/duration:.2f} MB/s", 'duration': f"{duration:.2f}s"
    })


# HTML 模板 (Tailwind CSS)
UPLOAD_PAGE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LeRobot 训练平台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>tailwind.config={darkMode:'class',theme:{extend:{colors:{d1:'#0d1117',d2:'#161b22',d3:'#21262d',border:'#30363d'}}}}</script>
    <style>body{font-family:ui-monospace,monospace}input[type=file]{display:none}::-webkit-scrollbar{width:6px}::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}</style>
</head>
<body class="bg-d1 text-gray-300 min-h-screen p-5">
<div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5 max-w-[1800px] mx-auto">

<!-- 上传区域 -->
<div class="bg-d2 border border-border rounded-xl p-6 shadow-2xl">
    <h1 class="text-2xl font-bold mb-2 bg-gradient-to-r from-blue-400 to-purple-500 bg-clip-text text-transparent">📁 数据集上传</h1>
    <p class="text-gray-500 text-sm mb-5">选择本地训练数据文件夹上传</p>
    
    <div id="uploadZone" class="border-2 border-dashed border-border rounded-lg p-8 text-center cursor-pointer bg-d3 hover:border-blue-500 hover:bg-blue-500/5 transition-all mb-4">
        <div class="text-4xl mb-3">📂</div>
        <div class="text-lg mb-1">点击选择文件夹</div>
        <div class="text-gray-500 text-sm">需包含 meta/、data/、videos/</div>
    </div>
    
    <div class="flex items-center gap-2 bg-yellow-500/10 border border-yellow-500 rounded-lg p-3 mb-4 text-sm">
        <span>💡</span><span>按 <kbd class="bg-d3 px-2 py-0.5 rounded text-blue-400">Ctrl+H</kbd> 显示隐藏文件夹</span>
    </div>
    
    <input type="file" id="folderInput" webkitdirectory directory multiple />
    
    <div id="selectedInfo" class="hidden bg-d3 border border-border rounded-lg p-4 mb-4">
        <h3 class="text-green-400 text-sm mb-2">✓ 已选择文件</h3>
        <div id="fileList" class="max-h-48 overflow-y-auto text-sm text-gray-500"></div>
    </div>
    
    <div id="progressContainer" class="hidden my-4">
        <div class="h-2 bg-d3 rounded overflow-hidden"><div id="progressFill" class="h-full bg-gradient-to-r from-blue-500 to-purple-500 w-0 transition-all"></div></div>
        <div class="flex justify-between text-xs text-gray-500 mt-2"><span id="progressPercent">0%</span><span id="progressDetail">准备中...</span></div>
    </div>
    
    <div class="flex gap-3 flex-wrap">
        <button id="uploadBtn" disabled class="px-5 py-2.5 rounded-lg font-medium bg-gradient-to-r from-blue-500 to-purple-500 text-white disabled:opacity-50 disabled:cursor-not-allowed hover:shadow-lg hover:shadow-blue-500/30 transition-all">🚀 开始上传</button>
        <button id="clearBtn" class="hidden px-4 py-2 rounded-lg bg-d3 border border-border hover:bg-border transition-all">🗑️ 清除</button>
    </div>
    
    <div id="result" class="hidden mt-4 p-4 rounded-lg"></div>
    
    <div class="mt-6 pt-4 border-t border-border text-sm text-gray-500">
        <p>📦 本地数据: <code class="bg-d3 px-1.5 rounded text-yellow-500">~/.cache/huggingface/lerobot/</code></p>
    </div>
</div>

<!-- 训练区域 -->
<div class="bg-d2 border border-border rounded-xl p-6 shadow-2xl">
    <h1 class="text-2xl font-bold mb-2 bg-gradient-to-r from-green-400 to-blue-500 bg-clip-text text-transparent">🚀 模型训练</h1>
    <p class="text-gray-500 text-sm mb-5">选择数据集和算法开始训练</p>
    
    <div class="mb-4">
        <label class="block text-sm text-gray-500 mb-1.5">选择数据集</label>
        <div class="flex gap-2">
            <select id="datasetSelect" class="flex-1 bg-d3 border border-border rounded-lg px-3 py-2.5 text-sm focus:border-blue-500 outline-none"><option>加载中...</option></select>
            <button id="refreshDatasets" class="px-3 py-2 bg-d3 border border-border rounded-lg hover:bg-border">🔄</button>
        </div>
    </div>
    
    <div class="mb-4">
        <label class="block text-sm text-gray-500 mb-1.5">选择算法</label>
        <select id="algorithmSelect" class="w-full bg-d3 border border-border rounded-lg px-3 py-2.5 text-sm focus:border-blue-500 outline-none">
            <option value="act">ACT</option><option value="diffusion">Diffusion</option><option value="smolvla">SmolVLA</option>
            <option value="pi05">Pi0.5 (单卡)</option><option value="pi05_multi">Pi0.5 (多卡)</option>
        </select>
    </div>
    
    <div class="bg-d1 border border-border rounded-lg p-3 mb-4">
        <label class="block text-xs text-gray-500 mb-2">训练命令 (可编辑)</label>
        <textarea id="commandTextarea" class="w-full h-48 bg-d3 border border-border rounded-lg p-3 text-xs resize-y focus:border-blue-500 outline-none" placeholder="选择数据集和算法后自动生成..."></textarea>
    </div>
    
    <div class="flex items-center gap-3 flex-wrap">
        <button id="startTrainingBtn" disabled class="px-5 py-2.5 rounded-lg font-medium bg-gradient-to-r from-blue-500 to-purple-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">▶️ 开始训练</button>
        <button id="stopTrainingBtn" class="hidden px-4 py-2 rounded-lg bg-red-500 text-white hover:bg-red-600">⏹️ 停止</button>
        <label class="ml-auto flex items-center gap-1.5 text-sm text-yellow-500 cursor-pointer"><input type="checkbox" id="shutdownAfter" class="w-4 h-4"><span>⚡ 训练后关机</span></label>
    </div>
    
    <div class="bg-d1 border border-border rounded-lg mt-4">
        <div class="flex justify-between items-center px-3 py-2 border-b border-border">
            <span class="text-sm">📋 训练日志</span>
            <span id="trainingStatus" class="text-xs px-2.5 py-1 rounded-full bg-d3 text-gray-500">空闲</span>
        </div>
        <div id="logContent" class="h-72 overflow-y-auto p-3 text-xs font-mono"><div class="text-gray-500">等待开始训练...</div></div>
    </div>
</div>

<!-- 下载区域 -->
<div class="bg-d2 border border-border rounded-xl p-6 shadow-2xl">
    <h1 class="text-2xl font-bold mb-2 bg-gradient-to-r from-yellow-500 to-red-500 bg-clip-text text-transparent">📦 模型下载</h1>
    <p class="text-gray-500 text-sm mb-5">下载训练完成的模型</p>
    
    <div class="flex justify-between items-center mb-4">
        <span class="text-sm text-gray-500">可用模型</span>
        <button id="refreshModels" class="px-3 py-1.5 text-sm bg-d3 border border-border rounded-lg hover:bg-border">🔄 刷新</button>
    </div>
    
    <div id="modelList" class="max-h-[500px] overflow-y-auto"><div class="text-center text-gray-500 py-10">加载中...</div></div>
</div>

</div>

<script>
const $ = id => document.getElementById(id);
const uploadZone=$('uploadZone'),folderInput=$('folderInput'),selectedInfo=$('selectedInfo'),fileList=$('fileList'),
      uploadBtn=$('uploadBtn'),clearBtn=$('clearBtn'),progressContainer=$('progressContainer'),progressFill=$('progressFill'),
      progressPercent=$('progressPercent'),progressDetail=$('progressDetail'),result=$('result'),
      datasetSelect=$('datasetSelect'),algorithmSelect=$('algorithmSelect'),commandTextarea=$('commandTextarea'),
      startTrainingBtn=$('startTrainingBtn'),stopTrainingBtn=$('stopTrainingBtn'),shutdownAfter=$('shutdownAfter'),
      trainingStatus=$('trainingStatus'),logContent=$('logContent'),modelList=$('modelList');

let selectedFiles=[],relativePaths=[],lastUploadedDataset=null,currentTaskId=null,logPollingInterval=null,lastLogIndex=0;

// 算法模板
const templates={
    act:(n,p)=>`lerobot-train --dataset.repo_id=mylerobot --dataset.root=${p} --policy.type=act --output_dir=~/data/output/act_${n}_model --job_name=${n}_job --policy.device=cuda --wandb.enable=false --steps=1000 --batch_size=16 --save_freq=10000 --policy.push_to_hub=false`,
    diffusion:(n,p)=>`lerobot-train --dataset.repo_id=mylerobot --dataset.root=${p} --policy.type=diffusion --output_dir=~/data/output/diffusion_${n}_model --job_name=${n}_job --policy.device=cuda --wandb.enable=false --steps=1000 --batch_size=16 --save_freq=10000 --policy.push_to_hub=false`,
    smolvla:(n,p)=>`lerobot-train --dataset.repo_id=mylerobot --dataset.root=${p} --policy.type=smolvla --output_dir=~/data/output/smolvla_${n}_model --job_name=${n}_job --policy.device=cuda --wandb.enable=false --steps=1000 --batch_size=16 --save_freq=10000 --policy.push_to_hub=false`,
    pi05:(n,p)=>`lerobot-train --dataset.repo_id=mylerobot --dataset.root=${p} --policy.type=pi05 --output_dir=~/data/output/pi05_${n}_model --job_name=${n}_job --policy.device=cuda --wandb.enable=false --steps=1000 --batch_size=16 --save_freq=10000 --policy.pretrained_path=~/data/models/pi05_base --policy.gradient_checkpointing=true --policy.dtype=bfloat16 --policy.push_to_hub=false`,
    pi05_multi:(n,p)=>`accelerate launch --multi_gpu --num_processes=2 --mixed_precision=bf16 $(which lerobot-train) --dataset.repo_id=mylerobot --dataset.root=${p} --policy.type=pi05 --output_dir=~/data/output/pi05_${n}_model --job_name=${n}_job --policy.device=cuda --wandb.enable=false --steps=1000 --batch_size=16 --save_freq=10000 --policy.pretrained_path=/root/data/models/pi05_base --policy.gradient_checkpointing=true --policy.dtype=bfloat16 --policy.push_to_hub=false`
};

// 上传功能
uploadZone.onclick=()=>folderInput.click();
folderInput.onchange=e=>{selectedFiles=Array.from(e.target.files);relativePaths=selectedFiles.map(f=>f.webkitRelativePath);updateFileList()};

function updateFileList(){
    if(!selectedFiles.length){selectedInfo.classList.add('hidden');uploadBtn.disabled=true;clearBtn.classList.add('hidden');return}
    selectedInfo.classList.remove('hidden');clearBtn.classList.remove('hidden');
    const size=(selectedFiles.reduce((s,f)=>s+f.size,0)/1024/1024).toFixed(2);
    const hasMeta=relativePaths.some(p=>p.split('/')[1]==='meta');
    const hasData=relativePaths.some(p=>p.split('/')[1]==='data');
    const hasVideos=relativePaths.some(p=>p.split('/')[1]==='videos');
    const valid=hasMeta&&hasData&&hasVideos;
    uploadBtn.disabled=!valid;
    let html=valid?'<div class="text-green-400 p-2 bg-green-500/10 rounded mb-2">✓ 目录结构正确</div>':
        `<div class="text-red-400 p-2 bg-red-500/10 rounded mb-2">⚠ 缺少: ${[!hasMeta&&'meta',!hasData&&'data',!hasVideos&&'videos'].filter(Boolean).join(', ')}</div>`;
    html+=`<div class="mb-2">${selectedFiles.length}个文件, ${size} MB</div>`;
    relativePaths.slice(0,15).forEach(p=>html+=`<div class="py-0.5 border-b border-border/50 truncate">${p}</div>`);
    if(relativePaths.length>15)html+=`<div class="text-yellow-500 py-1">...还有${relativePaths.length-15}个</div>`;
    fileList.innerHTML=html;
}

clearBtn.onclick=()=>{selectedFiles=[];relativePaths=[];folderInput.value='';updateFileList();result.classList.add('hidden')};

uploadBtn.onclick=async()=>{
    if(!selectedFiles.length)return;
    uploadBtn.disabled=true;progressContainer.classList.remove('hidden');result.classList.add('hidden');
    const start=Date.now(),total=selectedFiles.reduce((s,f)=>s+f.size,0);
    const form=new FormData();
    selectedFiles.forEach((f,i)=>{form.append('files',f);form.append('paths',relativePaths[i])});
    const xhr=new XMLHttpRequest();
    xhr.upload.onprogress=e=>{if(e.lengthComputable){
        const pct=Math.round(e.loaded/e.total*100);progressFill.style.width=pct+'%';progressPercent.textContent=pct+'%';
        const spd=((e.loaded/1024/1024)/((Date.now()-start)/1000)).toFixed(2);
        progressDetail.textContent=`${(e.loaded/1024/1024).toFixed(1)}/${(e.total/1024/1024).toFixed(1)} MB (${spd} MB/s)`;
    }};
    xhr.onload=()=>{
        const dur=(Date.now()-start)/1000,spd=(total/1024/1024/dur).toFixed(2);
        if(xhr.status===200){
            const r=JSON.parse(xhr.responseText);
            result.className='block mt-4 p-4 rounded-lg bg-green-500/10 border border-green-500 text-green-400';
            result.innerHTML=`<b>✓ 上传成功!</b><br>路径: <code class="bg-d3 px-1 rounded text-xs">${r.upload_path}</code><br>文件: ${r.total_files} | 大小: ${r.total_size} | 速度: ${spd} MB/s`;
            lastUploadedDataset=r.root_folder;loadDatasets();
        }else{result.className='block mt-4 p-4 rounded-lg bg-red-500/10 border border-red-500 text-red-400';result.innerHTML='上传失败'}
        uploadBtn.disabled=false;
    };
    xhr.open('POST','/upload_folder');xhr.send(form);
};

// 训练功能
async function loadDatasets(){
    try{
        const r=await(await fetch('/list_datasets')).json();
        datasetSelect.innerHTML=r.datasets.length?'':'<option>暂无数据集</option>';
        r.datasets.forEach(d=>{const o=document.createElement('option');o.value=d.path;o.dataset.name=d.name;o.textContent=d.name+(d.is_valid?' ✓':' ⚠');datasetSelect.appendChild(o)});
        if(lastUploadedDataset){for(let i=0;i<datasetSelect.options.length;i++)if(datasetSelect.options[i].dataset.name===lastUploadedDataset){datasetSelect.selectedIndex=i;break}lastUploadedDataset=null}
        updateCommand();
    }catch(e){datasetSelect.innerHTML='<option>加载失败</option>'}
}

function updateCommand(){
    const path=datasetSelect.value,name=datasetSelect.options[datasetSelect.selectedIndex]?.dataset?.name||'',algo=algorithmSelect.value;
    if(!path){commandTextarea.value='';startTrainingBtn.disabled=true;return}
    commandTextarea.value=templates[algo]?.(name,path)||'';startTrainingBtn.disabled=false;
}

datasetSelect.onchange=updateCommand;algorithmSelect.onchange=updateCommand;
$('refreshDatasets').onclick=loadDatasets;

startTrainingBtn.onclick=async()=>{
    const cmd=commandTextarea.value.trim();if(!cmd)return;
    if(shutdownAfter.checked&&!confirm('确定训练后关机?'))return;
    startTrainingBtn.disabled=true;currentTaskId='train_'+Date.now();lastLogIndex=0;
    try{
        const r=await(await fetch('/start_training',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd,task_id:currentTaskId,shutdown_after:shutdownAfter.checked})})).json();
        if(r.success){
            startTrainingBtn.classList.add('hidden');stopTrainingBtn.classList.remove('hidden');shutdownAfter.disabled=true;
            trainingStatus.className='text-xs px-2.5 py-1 rounded-full bg-blue-500/20 text-blue-400';
            trainingStatus.textContent=shutdownAfter.checked?'训练中(完成后关机)':'训练中...';
            logContent.innerHTML='<div>训练已启动...</div>';startLogPolling();
        }else{alert(r.error);startTrainingBtn.disabled=false}
    }catch(e){alert(e);startTrainingBtn.disabled=false}
};

stopTrainingBtn.onclick=async()=>{
    if(!currentTaskId)return;
    await fetch('/stop_training',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:currentTaskId})});
    stopLogPolling();trainingStatus.className='text-xs px-2.5 py-1 rounded-full bg-red-500/20 text-red-400';trainingStatus.textContent='已停止';
    startTrainingBtn.classList.remove('hidden');startTrainingBtn.disabled=false;stopTrainingBtn.classList.add('hidden');shutdownAfter.disabled=false;
};

function startLogPolling(){
    if(logPollingInterval)clearInterval(logPollingInterval);
    logPollingInterval=setInterval(async()=>{
        if(!currentTaskId)return;
        try{
            const r=await(await fetch(`/training_status?task_id=${currentTaskId}&last_index=${lastLogIndex}`)).json();
            if(r.logs?.length){r.logs.forEach(l=>{const d=document.createElement('div');d.innerHTML=`<span class="text-blue-400">[${l.time}]</span> ${l.message.replace(/</g,'&lt;')}`;logContent.appendChild(d)});logContent.scrollTop=logContent.scrollHeight;lastLogIndex=r.last_index}
            if(!r.is_running){
                stopLogPolling();
                trainingStatus.className=`text-xs px-2.5 py-1 rounded-full ${r.exit_code===0?'bg-green-500/20 text-green-400':'bg-red-500/20 text-red-400'}`;
                trainingStatus.textContent=r.exit_code===0?'完成':`退出:${r.exit_code}`;
                startTrainingBtn.classList.remove('hidden');startTrainingBtn.disabled=false;stopTrainingBtn.classList.add('hidden');shutdownAfter.disabled=false;loadModels();
            }
        }catch(e){}
    },1000);
}
function stopLogPolling(){if(logPollingInterval){clearInterval(logPollingInterval);logPollingInterval=null}}

// 下载功能
async function loadModels(){
    try{
        const r=await(await fetch('/list_models')).json();
        modelList.innerHTML=r.models?.length?'':'<div class="text-center text-gray-500 py-10">📭 暂无模型</div>';
        r.models?.forEach(m=>{
            const d=document.createElement('div');
            d.className='flex justify-between items-center p-3 bg-d3 border border-border rounded-lg mb-2 hover:border-blue-500 transition-all';
            d.innerHTML=`<div class="min-w-0 flex-1"><div class="truncate">📦 ${m.name}</div><div class="text-xs text-gray-500">${m.size_mb} | ${m.modified_time}</div></div><button onclick="location.href='/download_model?filename=${encodeURIComponent(m.name)}'" class="ml-3 px-3 py-1.5 bg-green-500 text-white text-sm rounded-lg hover:bg-green-600">⬇️</button>`;
            modelList.appendChild(d);
        });
    }catch(e){modelList.innerHTML='<div class="text-center text-red-400 py-10">加载失败</div>'}
}
$('refreshModels').onclick=loadModels;

loadDatasets();loadModels();
</script>
</body>
</html>
'''


@app.route('/', methods=['GET'])
def index():
    """首页"""
    return render_template_string(UPLOAD_PAGE_HTML, upload_folder=UPLOAD_FOLDER, download_folder=DOWNLOAD_FOLDER)


if __name__ == '__main__':
    ensure_directories()
    print("LeRobot 训练平台启动中...")
    print(f"数据集目录: {UPLOAD_FOLDER}")
    print(f"模型目录: {DOWNLOAD_TEMP_FOLDER}")
    print("访问: http://0.0.0.0:7001")
    app.run(host='0.0.0.0', port=7001, debug=True, use_reloader=False)
