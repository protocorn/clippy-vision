"""Parser for Ollama /api/pull streaming responses.

Ollama's /api/pull endpoint returns newline-delimited JSON objects with fields:
- status: string describing the current operation
- digest: hash of the layer being pulled (may differ across lines)
- total: total bytes for this layer
- completed: bytes downloaded so far for this layer
- error: error message if the pull failed

The parser aggregates progress across multiple layers and handles incomplete lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class PullProgress:
    """Progress update from an Ollama model pull."""
    
    status: str
    """Human-readable status message."""
    
    percent: float
    """Overall completion percentage (0-100)."""
    
    downloaded_mb: float
    """Total MB downloaded across all layers."""
    
    total_mb: float
    """Total MB to download (0 if not yet known)."""
    
    error: str | None = None
    """Error message if the pull failed."""
    
    is_complete: bool = False
    """True when the pull has finished successfully."""


class OllamaPullParser:
    """Streaming parser for Ollama /api/pull responses.
    
    Example usage:
        parser = OllamaPullParser()
        for chunk in stream:
            for progress in parser.feed(chunk):
                print(f"{progress.percent:.1f}% - {progress.downloaded_mb:.1f} / {progress.total_mb:.1f} MB")
    """
    
    def __init__(self):
        self._buffer = ""
        self._layers: dict[str, tuple[int, int]] = {}  # digest -> (completed, total)
        self._last_status = ""
        self._error: str | None = None
        self._complete = False
    
    def feed(self, chunk: str) -> list[PullProgress]:
        """Feed a chunk of data and return progress updates.
        
        Args:
            chunk: Raw text chunk from the stream (may contain partial lines).
            
        Returns:
            List of PullProgress objects for each complete JSON line parsed.
        """
        results = []
        
        if self._complete or self._error:
            return results
        
        self._buffer += chunk
        
        # Process complete lines
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            line = line.strip()
            
            if not line:
                continue
            
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Skip malformed lines
                continue
            
            progress = self._process_line(data)
            if progress:
                results.append(progress)
        
        return results
    
    def _process_line(self, data: dict) -> PullProgress | None:
        """Process a single JSON object from the stream."""
        # Check for errors first
        if 'error' in data and data['error']:
            self._error = str(data['error'])
            return self._make_progress(error=self._error)
        
        # Update status
        status = data.get('status', '')
        if status:
            self._last_status = status
        
        # Check for completion
        if status and 'success' in status.lower():
            self._complete = True
            return self._make_progress(is_complete=True)
        
        # Update layer progress
        digest = data.get('digest')
        if digest:
            completed = data.get('completed', 0)
            total = data.get('total', 0)
            
            # Only update if we have meaningful values
            if isinstance(completed, (int, float)) and isinstance(total, (int, float)):
                self._layers[digest] = (int(completed), int(total))
        
        # Return progress update if we have any data
        if self._layers or self._last_status:
            return self._make_progress()
        
        return None
    
    def _make_progress(
        self,
        error: str | None = None,
        is_complete: bool = False
    ) -> PullProgress:
        """Create a PullProgress object from current state."""
        total_downloaded = sum(completed for completed, _ in self._layers.values())
        total_size = sum(total for _, total in self._layers.values())
        
        # Calculate percentage
        if total_size > 0:
            percent = (total_downloaded / total_size) * 100.0
        elif is_complete:
            percent = 100.0
        else:
            percent = 0.0
        
        # Convert to MB
        downloaded_mb = total_downloaded / (1024 * 1024)
        total_mb = total_size / (1024 * 1024)
        
        return PullProgress(
            status=self._last_status or 'Downloading',
            percent=min(100.0, percent),
            downloaded_mb=downloaded_mb,
            total_mb=total_mb,
            error=error or self._error,
            is_complete=is_complete or self._complete
        )
    
    def finish(self) -> PullProgress:
        """Get final progress state (call after stream ends)."""
        # Process any remaining buffered data
        if self._buffer.strip():
            # Add a newline to process the buffered line
            remaining = self.feed('\n')
            if remaining:
                return remaining[-1]
        
        return self._make_progress()
