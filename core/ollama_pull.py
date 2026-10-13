"""CLI tool for pulling Ollama models with live progress reporting.

This script wraps the Ollama /api/pull endpoint and emits progress updates
as JSON lines that can be consumed by the Electron setup wizard.
"""

from __future__ import annotations

import argparse
import codecs
import json
import sys
import urllib.request
import urllib.error

from core.ollama_client import base_url, OllamaError, OllamaUnavailable, describe_http_error
from core.ollama_pull_parser import OllamaPullParser


def pull_model(model: str) -> int:
    """Pull an Ollama model and emit progress to stdout.
    
    Args:
        model: Model name/tag to pull (e.g., 'qwen3:8b')
        
    Returns:
        Exit code (0 on success, non-zero on error)
    """
    parser = OllamaPullParser()
    url = f"{base_url()}/api/pull"
    
    # Prepare the pull request
    data = json.dumps({"name": model}).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    try:
        # Open the streaming response
        with urllib.request.urlopen(req, timeout=600) as response:
            # Use an incremental decoder to handle multibyte UTF-8 characters
            # split across chunk boundaries
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            
            # Read the stream in chunks
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                
                # Decode incrementally (buffers partial multibyte sequences)
                text = decoder.decode(chunk, False)
                progress_updates = parser.feed(text)
                
                # Emit each progress update as a JSON line
                for progress in progress_updates:
                    output = {
                        'status': progress.status,
                        'percent': round(progress.percent, 1),
                        'downloaded_mb': round(progress.downloaded_mb, 2),
                        'total_mb': round(progress.total_mb, 2),
                        'error': progress.error,
                        'complete': progress.is_complete
                    }
                    print(json.dumps(output), flush=True)
                    
                    # If there's an error or completion, we're done
                    if progress.error:
                        return 1
                    if progress.is_complete:
                        return 0
            
            # Flush any remaining bytes in the decoder
            final_text = decoder.decode(b'', True)
            if final_text:
                parser.feed(final_text)
        
        # Process any remaining buffered data
        final = parser.finish()
        output = {
            'status': final.status,
            'percent': round(final.percent, 1),
            'downloaded_mb': round(final.downloaded_mb, 2),
            'total_mb': round(final.total_mb, 2),
            'error': final.error,
            'complete': final.is_complete
        }
        print(json.dumps(output), flush=True)
        
        return 0 if final.is_complete and not final.error else 1
        
    except urllib.error.HTTPError as err:
        error_msg = describe_http_error(err)
        error_output = {
            'status': 'error',
            'percent': 0.0,
            'downloaded_mb': 0.0,
            'total_mb': 0.0,
            'error': f"HTTP error: {error_msg}",
            'complete': False
        }
        print(json.dumps(error_output), flush=True)
        return 1
        
    except urllib.error.URLError as err:
        error_output = {
            'status': 'error',
            'percent': 0.0,
            'downloaded_mb': 0.0,
            'total_mb': 0.0,
            'error': f"Ollama not reachable at {base_url()}: {err.reason}. Is Ollama running?",
            'complete': False
        }
        print(json.dumps(error_output), flush=True)
        return 1
        
    except Exception as err:
        error_output = {
            'status': 'error',
            'percent': 0.0,
            'downloaded_mb': 0.0,
            'total_mb': 0.0,
            'error': str(err),
            'complete': False
        }
        print(json.dumps(error_output), flush=True)
        return 1


def main():
    """Main entry point."""
    parser_arg = argparse.ArgumentParser(
        description='Pull an Ollama model with live progress updates'
    )
    parser_arg.add_argument(
        'model',
        help='Model name/tag to pull (e.g., qwen3:8b)'
    )
    
    args = parser_arg.parse_args()
    sys.exit(pull_model(args.model))


if __name__ == '__main__':
    main()
