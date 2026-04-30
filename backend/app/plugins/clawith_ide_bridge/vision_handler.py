"""Vision handler for IDEA plugin integration."""

import asyncio
import base64
import io
from typing import Optional
from loguru import logger

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    logger.warning("[IDE-Vision] Pillow not installed, image compression disabled")

# Max image size in bytes (5MB)
MAX_IMAGE_SIZE = 5 * 1024 * 1024

def compress_image_if_needed(base64_data: str, max_size: int = MAX_IMAGE_SIZE) -> str:
    """Compress image if it exceeds the size threshold.
    
    Args:
        base64_data: Base64 string or data URL
        max_size: Maximum size in bytes (default 5MB)
        
    Returns:
        Compressed data URL or original if no compression needed
    """
    if not HAS_PILLOW:
        return base64_data
    
    try:
        # Extract content
        is_data_url = base64_data.startswith("data:image")
        if is_data_url:
            header_end = base64_data.find(",")
            if header_end != -1:
                content = base64_data[header_end + 1:]
            else:
                return base64_data
        else:
            content = base64_data
        
        # Check size
        if len(content) <= max_size:
            return base64_data
        
        logger.info(f"[IDE-Vision] Compressing image ({len(content)} bytes > {max_size} bytes)")
        
        # Decode and compress
        image_data = base64.b64decode(content)
        image = Image.open(io.BytesIO(image_data))
        
        # Calculate resize ratio
        ratio = (max_size / len(content)) ** 0.5
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        
        # Re-encode
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        compressed_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        result = f"data:image/png;base64,{compressed_base64}"
        logger.info(f"[IDE-Vision] Compressed to {len(compressed_base64)} bytes")
        return result
        
    except Exception as e:
        logger.warning(f"[IDE-Vision] Compression failed: {e}, returning original")
        return base64_data

def process_ide_image(base64_data: str) -> Optional[str]:
    """Process Base64 image data from IDEA plugin.
    
    Args:
        base64_data: Raw Base64 string or data URL from the plugin.
        
    Returns:
        A formatted data URL suitable for LLM Vision API, or None if processing fails.
    """
    try:
        # If it's already a data URL, validate and optionally compress
        if base64_data.startswith("data:image"):
            return compress_image_if_needed(base64_data)
        
        # If it's raw Base64, assume PNG and wrap it
        processed_data = f"data:image/png;base64,{base64_data}"
        return compress_image_if_needed(processed_data)
    except Exception as e:
        logger.warning(f"[IDE-Vision] Failed to process image: {e}")
        return None


async def compress_image_if_needed_async(
    base64_data: str, max_size: int = MAX_IMAGE_SIZE
) -> str:
    """Run Pillow compression off the event loop (sync Image.open/resize)."""
    return await asyncio.to_thread(compress_image_if_needed, base64_data, max_size)


async def process_ide_image_async(base64_data: str) -> Optional[str]:
    """Async entry point for callers in asyncio contexts."""
    return await asyncio.to_thread(process_ide_image, base64_data)
