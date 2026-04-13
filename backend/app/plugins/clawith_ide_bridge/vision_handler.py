"""Vision handler for IDEA plugin integration."""

import base64
from typing import Optional


def process_ide_image(base64_data: str) -> Optional[str]:
    """Process Base64 image data from IDEA plugin.
    
    Args:
        base64_data: Raw Base64 string or data URL from the plugin.
        
    Returns:
        A formatted data URL suitable for LLM Vision API, or None if processing fails.
    """
    try:
        # If it's already a data URL, return as is (with validation)
        if base64_data.startswith("data:image"):
            return base64_data
        
        # If it's raw Base64, assume PNG and wrap it
        # We might want to compress it here if it's too large
        return f"data:image/png;base64,{base64_data}"
    except Exception as e:
        from loguru import logger
        logger.warning(f"[IDE-Vision] Failed to process image: {e}")
        return None
