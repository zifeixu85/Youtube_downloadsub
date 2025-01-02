from flask import Flask, render_template, request, jsonify, send_file, Response, g
from yt_dlp import YoutubeDL
import os
import shutil
from pathlib import Path
import browser_cookie3
import queue
import json
import zipfile
import io
import re
from datetime import datetime

app = Flask(__name__)
# 使用环境变量或生成随机密钥
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(24)

# 创建一个队列用于存储进度信息
progress_queue = queue.Queue()

def check_ffmpeg():
    # 在 Vercel 环境中总是返回 True
    return True

def get_channel_videos(channel_url):
    """使用yt-dlp获取频道视频列表"""
    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'cookiesfrombrowser': ('chrome',),
            'playlistend': 100,
            'ignoreerrors': True,
            'no_warnings': True
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            print(f"Fetching videos from: {channel_url}")
            playlist_url = f"{channel_url}/shorts?view=0&sort=p&shelf_id=0"
            channel_info = ydl.extract_info(playlist_url, download=False)
            
            if not channel_info or 'entries' not in channel_info:
                return None, "无法获取视频列表"
                
            videos = []
            for entry in channel_info.get('entries', []):
                if not entry:
                    continue
                    
                try:
                    video = {
                        'id': entry.get('id', ''),
                        'title': entry.get('title', '未知标题'),
                        'view_count': int(entry.get('view_count', 0))
                    }
                    videos.append(video)
                except Exception as e:
                    print(f"Error processing entry: {e}")
                    continue
            
            videos.sort(key=lambda x: x['view_count'], reverse=True)
            top_videos = videos[:20]
            
            print(f"Found {len(top_videos)} popular shorts")
            return top_videos, None
            
    except Exception as e:
        print(f"Error getting channel videos: {str(e)}")
        return None, str(e)

@app.route('/')
def index():
    # 移除 ffmpeg 检查
    return render_template('index.html')

# 使用g对象存储channel信息，避免使用session
def get_channel_name():
    if not hasattr(g, 'channel_name'):
        g.channel_name = 'unknown'
    return g.channel_name

def set_channel_name(name):
    g.channel_name = name

@app.route('/get_videos', methods=['POST'])
def get_videos():
    channel_url = request.form.get('channel_url')
    
    try:
        if '/channel/' not in channel_url and '/c/' not in channel_url and '/user/' not in channel_url and '@' not in channel_url:
            return jsonify({
                'success': False,
                'error': '请输入有效的YouTube频道URL'
            })

        # 提取channel名称
        channel_name = channel_url.split('/')[-1]
        if channel_name.startswith('@'):
            channel_name = channel_name[1:]  # 移除@符号
        
        # 保存channel名称
        set_channel_name(channel_name)

        videos, error = get_channel_videos(channel_url)
        if error:
            return jsonify({
                'success': False,
                'error': error
            })
            
        if not videos:
            return jsonify({
                'success': False,
                'error': '未找到任何shorts视频'
            })
            
        return jsonify({
            'success': True,
            'videos': videos
        })
            
    except Exception as e:
        print(f"Error in get_videos: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/download_progress')
def download_progress():
    def generate():
        while True:
            try:
                progress = progress_queue.get()
                yield f"data: {json.dumps(progress)}\n\n"
            except:
                break
    return Response(generate(), mimetype='text/event-stream')

def extract_english_subtitles(vtt_content):
    """提取VTT文件中的纯英文字幕"""
    lines = vtt_content.split('\n')
    english_lines = []
    current_text = []
    
    for line in lines:
        # 跳过VTT头部信息和时间戳行
        if ('WEBVTT' in line or 
            '-->' in line or 
            'Kind:' in line or 
            'Language:' in line or 
            not line.strip() or
            'align:' in line or
            'position:' in line):
            continue
            
        # 移除时间标记 <00:00:00.000>
        cleaned_line = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', line)
        # 移除样式标记 <c> </c>
        cleaned_line = re.sub(r'</?c>', '', cleaned_line)
        # 移除空行
        cleaned_line = cleaned_line.strip()
        
        if cleaned_line:
            current_text.append(cleaned_line)
    
    # 合并连续的字幕文本
    text = ' '.join(current_text)
    # 移除重复的空格
    text = ' '.join(text.split())
    return text

@app.route('/download_subtitles', methods=['POST'])
def download_subtitles():
    try:
        videos = request.json.get('videos', [])
        if not videos:
            return jsonify({
                'success': False,
                'error': '未选择任何视频'
            })

        temp_dir = Path('temp_subtitles')
        if temp_dir.exists():
            for file in temp_dir.glob('*'):
                file.unlink()
        else:
            temp_dir.mkdir(parents=True)

        total_videos = len(videos)
        failed_videos = []
        
        for index, video_id in enumerate(videos, 1):
            try:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                progress_queue.put({
                    'current': index,
                    'total': total_videos,
                    'message': f'正在处理第 {index}/{total_videos} 个视频...'
                })

                ydl_opts = {
                    'quiet': True,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['en'],
                    'skip_download': True,
                    'outtmpl': str(temp_dir / '%(title)s'),
                    'cookiesfrombrowser': ('chrome',),
                }

                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    if info:
                        title = info.get('title', video_id)
                        # 检查是否有字幕文件
                        srt_files = list(temp_dir.glob(f"{title}*.vtt"))
                        if srt_files:
                            for srt_file in srt_files:
                                with open(srt_file, 'r', encoding='utf-8') as f:
                                    content = f.read()
                                
                                # 提取纯英文字幕
                                english_subtitles = extract_english_subtitles(content)
                                
                                # 保存为txt文件，使用原始标题
                                txt_file = temp_dir / f'{title}.txt'
                                with open(txt_file, 'w', encoding='utf-8') as f:
                                    f.write(english_subtitles)
                                
                                # 删除原始字幕文件
                                srt_file.unlink()
                        else:
                            failed_videos.append(title)
            except Exception as e:
                print(f"Error processing video {video_id}: {e}")
                failed_videos.append(video_id)
                continue

        progress_queue.put({
            'current': total_videos,
            'total': total_videos,
            'message': '处理完成！'
        })

        if failed_videos:
            return jsonify({
                'success': False,
                'error': f'以下视频字幕下载失败: {", ".join(failed_videos)}'
            })

        return jsonify({'success': True})

    except Exception as e:
        print(f"Error in download_subtitles: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/download_zip')
def download_zip():
    try:
        # 获取当前日期
        current_date = datetime.now().strftime('%Y%m%d')
        
        # 生成zip文件名，移除channel_name部分
        zip_filename = f"ShortsSub_{current_date}.zip"
        
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            temp_dir = Path('temp_subtitles')
            if temp_dir.exists():
                for txt_file in temp_dir.glob('*.txt'):
                    zf.write(txt_file, txt_file.name)
        
        memory_file.seek(0)
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=zip_filename
        )
    except Exception as e:
        print(f"Error creating zip file: {e}")
        return jsonify({
            'success': False,
            'error': '创建下载文件失败'
        })

if __name__ == '__main__':
    app.run(debug=True)