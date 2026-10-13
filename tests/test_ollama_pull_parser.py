"""Unit tests for Ollama pull stream parser."""

import pytest

from core.ollama_pull_parser import OllamaPullParser, PullProgress


def test_single_layer_complete_download():
    """Test parsing a simple single-layer download."""
    parser = OllamaPullParser()
    
    # Simulate a single layer download with progress updates
    stream = [
        '{"status":"pulling manifest","digest":"sha256:abc123","total":0,"completed":0}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":262144}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":1048576}\n',
        '{"status":"success"}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) > 0
    
    # Check that we got progress updates
    progress_updates = [r for r in results if r.downloaded_mb > 0]
    assert len(progress_updates) > 0
    assert progress_updates[0].total_mb > 0
    
    # Check final update shows completion
    final = results[-1]
    assert final.is_complete
    assert final.percent == 100.0


def test_multi_layer_download():
    """Test parsing multiple layers with different digests."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
        '{"status":"downloading","digest":"sha256:layer2","total":2097152,"completed":1048576}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":1048576}\n',
        '{"status":"downloading","digest":"sha256:layer2","total":2097152,"completed":2097152}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) > 0
    
    # Final state should have both layers completed
    final = results[-1]
    # 1MB + 2MB = 3MB total
    assert abs(final.downloaded_mb - 3.0) < 0.01
    assert abs(final.total_mb - 3.0) < 0.01
    assert final.percent == 100.0


def test_missing_total_field():
    """Test handling of progress updates without total field."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"pulling manifest"}\n',
        '{"status":"downloading","digest":"sha256:layer1","completed":1048576}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    # Should still produce updates, but with 0 total
    assert len(results) > 0
    # When total is 0 or missing, we can't calculate percentage accurately
    assert results[-1].downloaded_mb > 0


def test_missing_completed_field():
    """Test handling of updates without completed field."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"downloading","digest":"sha256:layer1","total":1048576}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) > 0
    # First update should show 0 completed
    assert results[0].downloaded_mb == 0
    # Second update should show progress
    assert results[1].downloaded_mb > 0


def test_partial_line_buffering():
    """Test that partial lines are buffered correctly."""
    parser = OllamaPullParser()
    
    # Split a JSON line in the middle
    chunk1 = '{"status":"downloading","digest":"sha256:layer1",'
    chunk2 = '"total":1048576,"completed":524288}\n'
    
    # First chunk should produce no results (incomplete)
    results1 = list(parser.feed(chunk1))
    assert len(results1) == 0
    
    # Second chunk completes the line
    results2 = list(parser.feed(chunk2))
    assert len(results2) == 1
    assert results2[0].downloaded_mb > 0


def test_error_handling():
    """Test parsing error responses."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":262144}\n',
        '{"error":"network error: connection refused"}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) >= 2
    # Last result should have the error
    assert results[-1].error is not None
    assert "connection refused" in results[-1].error.lower()


def test_ollama_not_running_error():
    """Test parsing Ollama unavailable errors."""
    parser = OllamaPullParser()
    
    stream = [
        '{"error":"Get \\"http://localhost:11434/api/version\\": dial tcp [::1]:11434: connect: connection refused"}\n',
    ]
    
    results = list(parser.feed(stream[0]))
    
    assert len(results) == 1
    assert results[0].error is not None
    assert "connection refused" in results[0].error


def test_invalid_model_name_error():
    """Test parsing invalid model name errors."""
    parser = OllamaPullParser()
    
    stream = [
        '{"error":"pull model manifest: file does not exist"}\n',
    ]
    
    results = list(parser.feed(stream[0]))
    
    assert len(results) == 1
    assert results[0].error is not None
    assert "does not exist" in results[0].error


def test_malformed_json():
    """Test that malformed JSON lines are skipped gracefully."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":262144}\n',
        'not valid json at all\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    # Should have 2 valid results, skipping the malformed line
    assert len(results) == 2
    assert results[0].downloaded_mb > 0
    assert results[1].downloaded_mb > results[0].downloaded_mb


def test_empty_lines():
    """Test that empty lines are skipped."""
    parser = OllamaPullParser()
    
    stream = [
        '\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
        '\n',
        '\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) == 1
    assert results[0].downloaded_mb > 0


def test_status_messages():
    """Test that status messages are preserved."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"pulling manifest"}\n',
        '{"status":"downloading digestsha256:abc"}\n',
        '{"status":"verifying sha256 digest"}\n',
        '{"status":"writing manifest"}\n',
        '{"status":"success"}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) > 0
    # Status should be updated
    assert results[0].status == "pulling manifest"
    # Last one should be complete
    assert results[-1].is_complete


def test_multiple_chunks_in_one_feed():
    """Test feeding multiple complete lines at once."""
    parser = OllamaPullParser()
    
    # Multiple lines in a single chunk
    chunk = (
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":262144}\n'
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n'
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":1048576}\n'
    )
    
    results = list(parser.feed(chunk))
    
    assert len(results) == 3
    assert results[0].percent < results[1].percent < results[2].percent
    assert results[2].percent == 100.0


def test_finish_with_buffered_data():
    """Test finish() method processes remaining buffered data."""
    parser = OllamaPullParser()
    
    # Feed data without trailing newline
    parser.feed('{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}')
    
    # finish() should process the buffered line
    final = parser.finish()
    assert final.downloaded_mb > 0
    assert final.total_mb > 0


def test_realistic_ollama_stream():
    """Test with a realistic Ollama pull stream."""
    parser = OllamaPullParser()
    
    # Simulated real Ollama output for pulling qwen3:8b
    stream = [
        '{"status":"pulling manifest"}\n',
        '{"status":"pulling 6bea4096cf2e","digest":"sha256:6bea4096cf2e","total":4934988448,"completed":1073741824}\n',
        '{"status":"pulling 6bea4096cf2e","digest":"sha256:6bea4096cf2e","total":4934988448,"completed":2147483648}\n',
        '{"status":"pulling 6bea4096cf2e","digest":"sha256:6bea4096cf2e","total":4934988448,"completed":3221225472}\n',
        '{"status":"pulling 6bea4096cf2e","digest":"sha256:6bea4096cf2e","total":4934988448,"completed":4934988448}\n',
        '{"status":"verifying sha256 digest"}\n',
        '{"status":"writing manifest"}\n',
        '{"status":"removing any unused layers"}\n',
        '{"status":"success"}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    assert len(results) > 0
    
    # Check that we got progress updates
    download_results = [r for r in results if r.downloaded_mb > 0]
    assert len(download_results) > 0
    
    # Check the final result
    final = results[-1]
    assert final.is_complete
    assert final.percent == 100.0
    # qwen3:8b is about 4.7 GB
    assert final.total_mb > 4000


def test_no_feed_after_error():
    """Test that feeding stops after an error."""
    parser = OllamaPullParser()
    
    stream = [
        '{"error":"some error"}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    # Should only get the error result
    assert len(results) == 1
    assert results[0].error is not None


def test_no_feed_after_completion():
    """Test that feeding stops after completion."""
    parser = OllamaPullParser()
    
    stream = [
        '{"status":"success"}\n',
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":524288}\n',
    ]
    
    results = []
    for chunk in stream:
        for progress in parser.feed(chunk):
            results.append(progress)
    
    # Should only get the success result
    assert len(results) == 1
    assert results[0].is_complete


def test_percent_clamped_to_100():
    """Test that percentage never exceeds 100%."""
    parser = OllamaPullParser()
    
    # Feed data where completed exceeds total (edge case)
    stream = [
        '{"status":"downloading","digest":"sha256:layer1","total":1048576,"completed":2097152}\n',
    ]
    
    results = list(parser.feed(stream[0]))
    
    assert len(results) == 1
    # Percent should be clamped to 100
    assert results[0].percent <= 100.0


def test_multibyte_utf8_split_across_chunks():
    """Test handling of multibyte UTF-8 characters split across feed calls."""
    parser = OllamaPullParser()
    
    # Construct a JSON line with a multibyte UTF-8 character (emoji: 🚀 = F0 9F 9A 80)
    # Split it in the middle of the multibyte sequence
    json_line = '{"status":"downloading 🚀 model","digest":"sha256:layer1","total":1048576,"completed":524288}\n'
    
    # Split the string at a point that would split the multibyte character
    # when encoded to UTF-8 bytes
    encoded = json_line.encode('utf-8')
    
    # Find where the emoji bytes are
    emoji_start = encoded.find(b'\xf0\x9f\x9a\x80')
    
    # Split in the middle of the emoji (after the first byte of the 4-byte sequence)
    if emoji_start >= 0:
        split_point = emoji_start + 1
        chunk1 = encoded[:split_point].decode('utf-8', errors='ignore')
        chunk2 = encoded[split_point:].decode('utf-8', errors='ignore')
        
        # Note: This test demonstrates that the parser is resilient to
        # text corruption, but the actual fix is in ollama_pull.py which uses
        # an incremental decoder. Here we just verify the parser doesn't crash
        # when given already-corrupted input.
        results1 = parser.feed(chunk1)
        results2 = parser.feed(chunk2)
        
        # Should not crash and should produce some results
        assert isinstance(results1, list)
        assert isinstance(results2, list)


def test_line_buffering_with_split_json():
    """Test that partial JSON lines are buffered correctly across feeds."""
    parser = OllamaPullParser()
    
    # Create a JSON line and split it at various points
    json_str = '{"status":"downloading","digest":"sha256:abc123","total":1048576,"completed":524288}\n'
    
    # Split at character 50
    chunk1 = json_str[:50]
    chunk2 = json_str[50:]
    
    # First chunk should produce no results (incomplete line)
    results1 = parser.feed(chunk1)
    assert len(results1) == 0
    
    # Second chunk completes the line
    results2 = parser.feed(chunk2)
    assert len(results2) == 1
    assert results2[0].downloaded_mb > 0
