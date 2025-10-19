from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import uvicorn
import os
import shutil
from pathlib import Path
from typing import List, Dict, Any
import aiofiles
import json
from pyngrok import ngrok
import time
import atexit
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure ngrok
NGROK_AUTH_TOKEN = os.getenv('NGROK_AUTH_TOKEN')
if not NGROK_AUTH_TOKEN:
    raise ValueError("NGROK_AUTH_TOKEN not found in .env file")

# Configure server settings
SERVER_HOST = os.getenv('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.getenv('SERVER_PORT', '8000'))

app = FastAPI(title="Cloud Storage System")

# Configure storage directory
STORAGE_DIR = Path(os.getenv('STORAGE_DIR', 'storage'))
STORAGE_DIR.mkdir(exist_ok=True)

# Configure URL storage
URL_FILE = Path("ngrok_url.json")

def cleanup():
    """Cleanup function to run when server stops"""
    print("\nCleaning up ngrok tunnel...")
    try:
        ngrok.kill()  # Kill any running ngrok processes
        if URL_FILE.exists():
            URL_FILE.unlink()  # Delete the URL file
    except Exception as e:
        print(f"Error during cleanup: {e}")

# Register cleanup function
atexit.register(cleanup)

@app.on_event("startup")
async def startup_event():
    """Setup ngrok tunnel when server starts"""
    try:
        # Configure ngrok
        ngrok.set_auth_token(NGROK_AUTH_TOKEN)
        
        # Start ngrok tunnel
        http_tunnel = ngrok.connect(SERVER_PORT)
        public_url = http_tunnel.public_url
        
        # Save URL to file for client to read
        with open(URL_FILE, 'w') as f:
            json.dump({
                'url': public_url,
                'timestamp': time.time()
            }, f)
        
        print(f"\nCloud Storage System Server")
        print(f"=========================")
        print(f"Local URL: http://localhost:8000")
        print(f"Public URL: {public_url}")
        print(f"URL saved to: {URL_FILE}")
        print("\nPress Ctrl+C to stop the server")
        
    except Exception as e:
        print(f"Error setting up ngrok: {e}")
        raise

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

@app.get("/status")
async def server_status():
    """Get server status and public URL"""
    try:
        tunnels = ngrok.get_tunnels()
        return {
            "status": "running",
            "public_url": tunnels[0].public_url if tunnels else None
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

if __name__ == "__main__":
    try:
        print("\nStarting Cloud Storage System Server")
        print("=================================")
        print(f"Loading configuration from .env file")
        print(f"Server host: {SERVER_HOST}")
        print(f"Server port: {SERVER_PORT}")
        print(f"Storage directory: {STORAGE_DIR}")
        print(f"Ngrok token: {'Configured' if NGROK_AUTH_TOKEN else 'Missing'}\n")
        
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
    finally:
        cleanup()