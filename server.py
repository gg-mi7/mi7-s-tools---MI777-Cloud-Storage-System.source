from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import uvicorn
import os
import shutil
from pathlib import Path
from typing import List, Dict, Any
import aiofiles

app = FastAPI(title="Cloud Storage System")

# Configure storage directory
STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(exist_ok=True)

@app.get("/files")
async def list_files() -> List[Dict[str, Any]]:
    """List all files and directories in the storage"""
    items = []
    for path in STORAGE_DIR.rglob("*"):
        relative_path = str(path.relative_to(STORAGE_DIR))
        items.append({
            "path": relative_path,
            "is_directory": path.is_dir(),
            "size": 0 if path.is_dir() else path.stat().st_size,
            "modified": path.stat().st_mtime
        })
    return items

@app.post("/upload/{file_path:path}")
async def upload_file(file_path: str, file: UploadFile = None, is_directory: bool = Form(False)):
    """Upload a file or create a directory in the storage"""
    try:
        file_location = STORAGE_DIR / file_path
        file_location.parent.mkdir(parents=True, exist_ok=True)

        if is_directory:
            file_location.mkdir(exist_ok=True)
            return {"message": f"Directory {file_path} created successfully"}
        
        if not file:
            raise HTTPException(status_code=400, detail="No file provided for file upload")
            
        async with aiofiles.open(file_location, 'wb') as f:
            content = await file.read()
            await f.write(content)
        return {"message": f"File {file_path} uploaded successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{file_path:path}")
async def download_file(file_path: str):
    """Download a file from storage"""
    file_location = STORAGE_DIR / file_path
    if not file_location.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_location)

@app.delete("/delete/{file_path:path}")
async def delete_file(file_path: str):
    """Delete a file from storage"""
    file_location = STORAGE_DIR / file_path
    if not file_location.exists():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        if file_location.is_file():
            file_location.unlink()
        else:
            shutil.rmtree(file_location)
        return {"message": f"{file_path} deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)