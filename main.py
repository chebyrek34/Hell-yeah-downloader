import asyncio
import json
import os
import tempfile
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = tempfile.gettempdir()

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str

def get_yt_dlp_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "nocheckcertificate": True,
    }

def format_bytes(bytes_size: Optional[int]) -> str:
    if bytes_size is None:
        return "неизвестно"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest):
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL не может быть пустым")

    try:
        opts = get_yt_dlp_opts()
        opts["extract_flat"] = False
        
        with yt_dlp.YoutubeDL(opts) as ydl:
            logger.info(f"Analyzing URL: {url}")
            info = ydl.extract_info(url, download=False)

        platform = info.get("extractor", "unknown").split("+")[0]
        platform_map = {
            "youtube": "YouTube", "reddit": "Reddit", "twitter": "Twitter/X",
            "x": "Twitter/X", "pinterest": "Pinterest", "vk": "VK",
        }
        platform_display = platform_map.get(platform.lower(), platform.upper())

        thumbnail = info.get("thumbnail")
        if not thumbnail and info.get("thumbnails"):
            thumbnail = info["thumbnails"][-1].get("url")

        formats_list = []
        seen_heights = set()

        if "formats" in info:
            for fmt in info["formats"]:
                vcodec = fmt.get("vcodec")
                acodec = fmt.get("acodec")
                height = fmt.get("height")
                ext = fmt.get("ext")
                filesize = fmt.get("filesize") or fmt.get("file_size")

                # Только видеоформаты в MP4
                if vcodec == "none" or ext != "mp4" or not height:
                    continue

                # Убираем дубликаты
                if height in seen_heights:
                    continue
                seen_heights.add(height)

                format_label = f"{height}p MP4"
                size_str = f" (~{format_bytes(filesize)})" if filesize else ""
                
                formats_list.append({
                    "format_id": fmt["format_id"],
                    "label": format_label + size_str,
                    "height": height,
                    "ext": ext,
                    "has_video": True,
                    "has_audio": acodec != "none",
                })

        # Ищем лучший аудиоформат
        audio_format = None
        if "formats" in info:
            audio_formats = [f for f in info["formats"] if f.get("vcodec") == "none" and f.get("acodec") != "none"]
            if audio_formats:
                audio_format = max(audio_formats, key=lambda f: f.get("abr") or 0)

        if audio_format:
            filesize = audio_format.get("filesize") or audio_format.get("file_size")
            size_str = f" (~{format_bytes(filesize)})" if filesize else ""
            
            # Добавляем M4A
            formats_list.append({
                "format_id": audio_format["format_id"],
                "label": f"Только аудио (M4A){size_str}",
                "height": 0, "ext": "m4a", "has_video": False, "has_audio": True,
            })
            
            # Добавляем MP3 (конвертация)
            formats_list.append({
                "format_id": "mp3",
                "label": f"Только аудио (MP3){size_str}",
                "height": 0, "ext": "mp3", "has_video": False, "has_audio": True,
            })

        formats_list.sort(key=lambda x: x["height"], reverse=True)

        if not formats_list:
            raise HTTPException(status_code=400, detail="Не удалось найти доступные видеоформаты MP4")

        return {
            "platform": platform_display,
            "title": info.get("title", "Без названия"),
            "thumbnail": thumbnail,
            "duration": info.get("duration"),
            "formats": formats_list,
        }

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download error: {e}")
        raise HTTPException(status_code=400, detail="Не удалось получить информацию о видео. Проверьте ссылку.")
    except Exception as e:
        logger.error(f"Error analyzing URL: {e}")
        raise HTTPException(status_code=400, detail=f"Ошибка: {str(e)}")

@app.post("/api/download")
async def download_video(request: DownloadRequest):
    url = request.url.strip()
    format_id = request.format_id.strip()

    if not url or not format_id:
        raise HTTPException(status_code=400, detail="URL и format_id обязательны")

    try:
        temp_dir = tempfile.mkdtemp()
        
        opts = get_yt_dlp_opts()
        
        if format_id == "mp3":
            # Настройки для конвертации в MP3
            opts.update({
                "format": "bestaudio/best",
                "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            # Обычное скачивание видео
            opts.update({
                "format": f"{format_id}+bestaudio/best" if "audio" not in format_id.lower() else format_id,
                "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
            })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            
            # Если это MP3, yt-dlp меняет расширение после конвертации
            if format_id == "mp3":
                base_name = os.path.splitext(downloaded_file)[0]
                downloaded_file = base_name + ".mp3"
            else:
                # Проверка расширений для видео (после склейки)
                if not os.path.exists(downloaded_file):
                    base_name = os.path.splitext(downloaded_file)[0]
                    if os.path.exists(base_name + ".mp4"):
                        downloaded_file = base_name + ".mp4"
                    elif os.path.exists(base_name + ".mkv"):
                        downloaded_file = base_name + ".mkv"
                    elif os.path.exists(base_name + ".webm"):
                        downloaded_file = base_name + ".webm"

        if not os.path.exists(downloaded_file):
            raise Exception("Файл не найден после скачивания")

        file_size = os.path.getsize(downloaded_file)
        
        ext = os.path.splitext(downloaded_file)[1]
        filename = f"video{ext}"

        def iterfile():
            with open(downloaded_file, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            try:
                import shutil
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Failed to clean up {temp_dir}: {e}")

        if ext == ".mp4":
            media_type = "video/mp4"
        elif ext == ".m4a":
            media_type = "audio/mp4"
        elif ext == ".mp3":
            media_type = "audio/mpeg"
        elif ext == ".webm":
            media_type = "video/webm"
        elif ext == ".mkv":
            media_type = "video/x-matroska"
        else:
            media_type = "application/octet-stream"

        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size),
            },
        )

    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")

@app.get("/")
async def root():
    return {"message": "Video Downloader API", "version": "1.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)