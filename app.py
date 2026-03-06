"""
RTSP Camera Frame Drop Diagnostic
==================================
A Windows desktop tool that diagnoses the health of CCTV / IP cameras over
a network using FFmpeg or GStreamer as the back-end engine.

Features
--------
- Real-time metrics: frames received, estimated drops, bandwidth, FPS jitter,
  startup latency, missed RTP packets, stream health score (0–100).
- RTSP transport probing: tests TCP, UDP-unicast, and UDP-multicast so you
  know which paths work before committing to a full run.
- PDF + JSON reports with KPI cards, timeline charts, bandwidth distribution,
  frame-distribution pie chart, warning category chart, and a live snapshot.
- Optional FFplay or GStreamer side-by-side live preview window.
- No-audio (video-only) cameras are fully supported.
- Passwords containing reserved URL characters (e.g. ``@``) are
  automatically percent-encoded before they are passed to subprocesses.

Usage
-----
Run directly::

    python app.py

Or double-click ``run_diagnostic_tool.bat``.

See README.md for full documentation, screenshots, and deployment notes.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from statistics import mean, pstdev
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
except Exception:
    FPDF = None
    XPos = None
    YPos = None

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None


APP_TITLE = "RTSP Camera Frame Drop Diagnostic"
APP_VERSION = "1.1.0"
# BUILD_DATE is the release date for this version; bump together with APP_VERSION on each release.
BUILD_DATE = "2026-03-06"
MAX_WARNING_SAMPLES = 30
LIVE_CHART_MAX_POINTS = 180
DIAGNOSTIC_ENGINES = ("ffmpeg", "gstreamer")
RTSP_TRANSPORT_MODES = ("auto", "tcp", "udp", "udp_multicast")
RTSP_TRANSPORT_PROBE_CANDIDATES = ("tcp", "udp", "udp_multicast")


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        return {"creationflags": creationflags}
    return {}


def safe_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(mean(values))


def safe_stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return float(pstdev(values))


def safe_percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if pct <= 0:
        return float(min(values))
    if pct >= 100:
        return float(max(values))
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


def select_chart_indices(count: int, max_points: int) -> list[int]:
    if count <= 0:
        return []
    if count <= max_points:
        return list(range(count))
    if max_points <= 1:
        return [count - 1]
    return sorted({int(round(i * (count - 1) / (max_points - 1))) for i in range(max_points)})


def cumulative_to_interval(values: list[float]) -> list[float]:
    intervals: list[float] = []
    previous = 0.0
    for raw_value in values:
        current = max(0.0, float(raw_value))
        intervals.append(max(0.0, current - previous))
        previous = current
    return intervals


def parse_float_token(value: str) -> Optional[float]:
    if not value:
        return None
    token = str(value).strip().lower().replace("x", "")
    if token in {"n/a", "nan", "inf", "-inf"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", token)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_int_token(value: str) -> int:
    if value is None:
        return 0
    token = str(value).strip()
    match = re.search(r"-?\d+", token)
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def parse_bitrate_to_kbps(value: str) -> Optional[float]:
    if not value:
        return None
    token = str(value).strip().lower()
    if token in {"n/a", "nan", "inf", "-inf"}:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([kmg]?)(?:bits/s|bit/s|b/s)?", token)
    if not match:
        return None
    try:
        magnitude = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2)
    if unit == "g":
        return magnitude * 1_000_000.0
    if unit == "m":
        return magnitude * 1_000.0
    if unit == "k":
        return magnitude
    return magnitude / 1_000.0


def classify_warning(log_line: str) -> str:
    lowered = log_line.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "missed" in lowered and "packet" in lowered:
        return "packet_loss"
    if "decode" in lowered or "corrupt" in lowered or "invalid" in lowered:
        return "decode"
    if "rtsp" in lowered:
        return "rtsp_protocol"
    if "overrun" in lowered or "buffer" in lowered:
        return "buffering"
    if "failed" in lowered or "error" in lowered:
        return "error"
    return "other"


def extract_missed_packets(log_line: str) -> int:
    match = re.search(r"missed\s+(\d+)\s+packets?", log_line.lower())
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def compute_health_score(
    *,
    drop_rate_percent: float,
    fps_jitter_percent: float,
    warning_count: int,
    freeze_ratio_percent: float,
    startup_latency_sec: float,
    missed_packets: int,
) -> tuple[int, str]:
    score = 100.0
    score -= min(45.0, drop_rate_percent * 1.8)
    score -= min(15.0, fps_jitter_percent * 0.8)
    score -= min(12.0, warning_count * 0.7)
    score -= min(10.0, freeze_ratio_percent * 0.6)
    score -= min(10.0, missed_packets / 20.0)
    if startup_latency_sec > 3.0:
        score -= min(8.0, (startup_latency_sec - 3.0) * 1.5)

    score = max(0.0, min(100.0, score))
    score_int = int(round(score))
    if score_int >= 90:
        grade = "Excellent"
    elif score_int >= 75:
        grade = "Good"
    elif score_int >= 55:
        grade = "Fair"
    else:
        grade = "Poor"
    return score_int, grade


def capture_rtsp_snapshot(
    *,
    ffmpeg_path: str,
    rtsp_url: str,
    output_path: Path,
    transport: str = "tcp",
    timeout_sec: int = 20,
) -> tuple[bool, str]:
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"Unable to prepare snapshot folder: {exc}"

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rtsp_transport",
        transport,
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False, "Snapshot timeout while reading RTSP stream."
    except Exception as exc:
        return False, f"Snapshot command failed: {exc}"

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        return False, details or f"ffmpeg snapshot failed with code {result.returncode}"
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "Snapshot file was not created."
    return True, ""


def load_chart_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    font_candidates = []
    if os.name == "nt":
        windows_fonts = Path("C:/Windows/Fonts")
        font_candidates.extend(
            [
                windows_fonts / ("arialbd.ttf" if bold else "arial.ttf"),
                windows_fonts / ("segoeuib.ttf" if bold else "segoeui.ttf"),
            ]
        )
    for candidate in font_candidates:
        try:
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def nice_axis_max(value: float) -> float:
    value = max(1.0, float(value))
    magnitude = 10 ** max(0, len(str(int(value))) - 1)
    scaled = value / magnitude
    if scaled <= 1.5:
        nice = 2.0
    elif scaled <= 3.0:
        nice = 5.0
    elif scaled <= 7.0:
        nice = 10.0
    else:
        nice = 20.0
    return nice * magnitude


def draw_text(draw, xy: tuple[int, int], text: str, *, fill: str, font, anchor: str | None = None) -> None:
    if font is None:
        draw.text(xy, text, fill=fill, anchor=anchor)
    else:
        draw.text(xy, text, fill=fill, font=font, anchor=anchor)


def draw_simple_line(draw, points: list[tuple[int, int]], color: str, width: int = 2) -> None:
    if len(points) < 2:
        return
    draw.line(points, fill=color, width=width)


def generate_report_charts_pillow(report_data: dict, charts_dir: Path) -> dict[str, str]:
    if Image is None or ImageDraw is None:
        return {}

    timeline = report_data.get("timeline", [])
    if not timeline:
        return {}

    charts_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}

    x = [float(item.get("analysis_elapsed_sec", item.get("elapsed_sec", 0.0)) or 0.0) for item in timeline]
    frames = [float(item.get("frame", 0.0) or 0.0) for item in timeline]
    expected_frames = [float(item.get("expected_frames", 0.0) or 0.0) for item in timeline]
    drops = [float(item.get("estimated_drop_frames", 0.0) or 0.0) for item in timeline]
    drop_rate = [float(item.get("drop_rate_percent", 0.0) or 0.0) for item in timeline]
    bandwidth = [float(item.get("bandwidth_kbps_current", 0.0) or 0.0) for item in timeline]
    realtime_fps = [float(item.get("fps_realtime_num", 0.0) or 0.0) for item in timeline]
    health = [float(item.get("health_score", 0.0) or 0.0) for item in timeline]
    wall_elapsed = [
        float(item.get("wall_elapsed_sec", item.get("analysis_elapsed_sec", item.get("elapsed_sec", 0.0))) or 0.0)
        for item in timeline
    ]
    plot_indices = select_chart_indices(len(x), 44)
    x_plot = [x[index] for index in plot_indices]
    frames_plot = [frames[index] for index in plot_indices]
    expected_frames_plot = [expected_frames[index] for index in plot_indices]
    drops_plot = [drops[index] for index in plot_indices]
    drop_rate_plot = [drop_rate[index] for index in plot_indices]
    bandwidth_plot = [bandwidth[index] for index in plot_indices]
    received_interval_plot = cumulative_to_interval(frames_plot)
    dropped_interval_plot = cumulative_to_interval(drops_plot)

    title_font = load_chart_font(30, bold=True)
    body_font = load_chart_font(18)
    small_font = load_chart_font(14)
    tiny_font = load_chart_font(12)

    def new_canvas(title: str, width: int = 1500, height: int = 760, dark: bool = False):
        bg = "#071523" if dark else "#FFFFFF"
        img = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(img)
        draw_text(draw, (32, 26), title, fill="#F8FAFC" if dark else "#102235", font=title_font)
        return img, draw

    def plot_rect(dark: bool = False, legend: bool = False, two_panels: bool = False):
        if two_panels:
            return (95, 95, 1410, 335), (95, 410, 1410, 650)
        if legend:
            return (90, 90, 1260, 650)
        return (90, 90, 1410, 650)

    def draw_grid(draw, rect, *, dark: bool = False):
        left, top, right, bottom = rect
        grid_color = "#294056" if dark else "#D6E0EA"
        border_color = "#47607A" if dark else "#7B8EA3"
        draw.rectangle(rect, outline=border_color, width=2)
        for idx in range(1, 5):
            y_pos = int(top + ((bottom - top) * idx / 5.0))
            draw.line((left, y_pos, right, y_pos), fill=grid_color, width=1)

    def x_positions(rect, values: list[float]) -> list[int]:
        left, _, right, _ = rect
        if len(values) <= 1:
            return [int((left + right) / 2)] * len(values)
        span = max(values) - min(values)
        if span <= 0:
            span = float(len(values) - 1 or 1)
        positions = []
        for idx, value in enumerate(values):
            normalized = (value - min(values)) / span if span > 0 else (idx / max(1, len(values) - 1))
            positions.append(int(left + normalized * (right - left)))
        return positions

    def y_position(rect, value: float, max_value: float, min_value: float = 0.0) -> int:
        _, top, _, bottom = rect
        span = max(0.001, max_value - min_value)
        normalized = (value - min_value) / span
        normalized = max(0.0, min(1.0, normalized))
        return int(bottom - normalized * (bottom - top))

    def save_chart(img, output_name: str) -> None:
        output_path = charts_dir / output_name
        img.save(output_path)
        out[output_name.rsplit(".", 1)[0]] = str(output_path)

    # Live dashboard graph.
    img, draw = new_canvas("Live Dashboard Graph", dark=True)
    rect = plot_rect(dark=True, legend=True)
    draw_grid(draw, rect, dark=True)
    x_pos = x_positions(rect, x_plot)
    frame_axis_max = nice_axis_max(max(received_interval_plot + dropped_interval_plot + [1.0]))
    bw_axis_max = nice_axis_max(max(bandwidth_plot + [1.0]))
    bar_width = max(10, int((rect[2] - rect[0]) / max(24, len(x_pos) * 2)))
    for idx, xpos in enumerate(x_pos):
        rx_y = y_position(rect, received_interval_plot[idx], frame_axis_max)
        drop_y = y_position(rect, dropped_interval_plot[idx], frame_axis_max)
        draw.rectangle((xpos - bar_width, rx_y, xpos - 2, rect[3]), fill="#80ED99")
        draw.rectangle((xpos + 2, drop_y, xpos + bar_width, rect[3]), fill="#F94144")
    bw_points = [(xpos, y_position(rect, bandwidth_plot[idx], bw_axis_max)) for idx, xpos in enumerate(x_pos)]
    draw_simple_line(draw, bw_points, "#4CC9F0", width=3)
    for xpos, ypos in bw_points:
        draw.ellipse((xpos - 3, ypos - 3, xpos + 3, ypos + 3), fill="#4CC9F0")
    draw_text(draw, (46, rect[1]), f"{int(round(frame_axis_max))}", fill="#E0E1DD", font=small_font, anchor="ls")
    draw_text(draw, (46, rect[3]), "0", fill="#E0E1DD", font=small_font, anchor="ls")
    draw_text(draw, (rect[2] + 8, rect[1]), f"{int(round(bw_axis_max))} kbps", fill="#E0E1DD", font=small_font, anchor="ls")
    draw_text(draw, (rect[2] + 8, rect[3]), "0 kbps", fill="#E0E1DD", font=small_font, anchor="ls")
    legend_x = 1310
    legend_items = [
        ("#80ED99", "Frames Received"),
        ("#F94144", "Dropped Frames"),
        ("#4CC9F0", "Bandwidth"),
    ]
    draw_text(draw, (legend_x, 120), "Key", fill="#E0E1DD", font=body_font)
    for idx, (color, label) in enumerate(legend_items):
        y_pos = 165 + idx * 55
        draw.rectangle((legend_x, y_pos, legend_x + 24, y_pos + 24), fill=color)
        draw_text(draw, (legend_x + 38, y_pos + 2), label, fill="#E0E1DD", font=small_font)
    save_chart(img, "timeline_frames.png")

    # Expected vs received.
    img, draw = new_canvas("Expected vs Received Frames")
    rect = plot_rect()
    draw_grid(draw, rect)
    x_pos = x_positions(rect, x_plot)
    frame_max = nice_axis_max(max(expected_frames_plot + frames_plot + [1.0]))
    received_points = [(xpos, y_position(rect, frames_plot[idx], frame_max)) for idx, xpos in enumerate(x_pos)]
    expected_points = [(xpos, y_position(rect, expected_frames_plot[idx], frame_max)) for idx, xpos in enumerate(x_pos)]
    draw_simple_line(draw, expected_points, "#264653", width=3)
    draw_simple_line(draw, received_points, "#2A9D8F", width=4)
    draw_text(draw, (rect[0], rect[1] - 28), "Expected", fill="#264653", font=small_font)
    draw_text(draw, (rect[0] + 140, rect[1] - 28), "Received", fill="#2A9D8F", font=small_font)
    save_chart(img, "expected_vs_received.png")

    # Quality timeline with two panels.
    img, draw = new_canvas("Quality Timeline")
    rect_top, rect_bottom = plot_rect(two_panels=True)
    draw_grid(draw, rect_top)
    draw_grid(draw, rect_bottom)
    x_all = x_positions(rect_top, x)
    fps_max = nice_axis_max(max(realtime_fps + [1.0]))
    fps_points = [(x_all[idx], y_position(rect_top, realtime_fps[idx], fps_max)) for idx in range(len(x_all))]
    health_points = [(x_all[idx], y_position(rect_bottom, health[idx], 100.0)) for idx in range(len(x_all))]
    draw_simple_line(draw, fps_points, "#2A9D8F", width=3)
    draw_simple_line(draw, health_points, "#6A4C93", width=3)
    draw_text(draw, (rect_top[0], rect_top[1] - 24), "Realtime FPS", fill="#102235", font=small_font)
    draw_text(draw, (rect_bottom[0], rect_bottom[1] - 24), "Health Score", fill="#102235", font=small_font)
    save_chart(img, "timeline_performance.png")

    # Drop timeline.
    img, draw = new_canvas("Drop Timeline")
    rect_top, rect_bottom = plot_rect(two_panels=True)
    draw_grid(draw, rect_top)
    draw_grid(draw, rect_bottom)
    x_all = x_positions(rect_top, x)
    drop_max = nice_axis_max(max(drops + [1.0]))
    drop_points = [(x_all[idx], y_position(rect_top, drops[idx], drop_max)) for idx in range(len(x_all))]
    draw_simple_line(draw, drop_points, "#E63946", width=3)
    draw_text(draw, (rect_top[0], rect_top[1] - 24), "Cumulative Drops", fill="#102235", font=small_font)
    x_pos = x_positions(rect_bottom, x_plot)
    drop_rate_max = nice_axis_max(max(drop_rate_plot + dropped_interval_plot + [1.0]))
    for idx, xpos in enumerate(x_pos):
        top_y = y_position(rect_bottom, dropped_interval_plot[idx], drop_rate_max)
        draw.rectangle((xpos - 8, top_y, xpos + 8, rect_bottom[3]), fill="#F94144")
    drop_rate_points = [(xpos, y_position(rect_bottom, drop_rate_plot[idx], drop_rate_max)) for idx, xpos in enumerate(x_pos)]
    draw_simple_line(draw, drop_rate_points, "#6D597A", width=3)
    draw_text(draw, (rect_bottom[0], rect_bottom[1] - 24), "Drops/Sample + Drop Rate %", fill="#102235", font=small_font)
    save_chart(img, "drop_timeline.png")

    if any(value > 0 for value in bandwidth):
        img, draw = new_canvas("Bandwidth Distribution")
        rect = plot_rect()
        draw_grid(draw, rect)
        positive_bandwidth = [value for value in bandwidth if value > 0]
        bin_count = min(12, max(5, len(positive_bandwidth) // 3))
        bw_min = min(positive_bandwidth)
        bw_max = max(positive_bandwidth)
        bw_span = max(1.0, bw_max - bw_min)
        bins = [0] * bin_count
        for value in positive_bandwidth:
            idx = min(bin_count - 1, int(((value - bw_min) / bw_span) * bin_count))
            bins[idx] += 1
        bar_space = (rect[2] - rect[0]) / max(1, bin_count)
        count_max = nice_axis_max(max(bins + [1]))
        for idx, count in enumerate(bins):
            left = int(rect[0] + idx * bar_space + 6)
            right = int(rect[0] + (idx + 1) * bar_space - 6)
            top = y_position(rect, count, count_max)
            draw.rectangle((left, top, right, rect[3]), fill="#4CC9F0", outline="#264653")
        save_chart(img, "bandwidth_distribution.png")

    if any(abs(wall - media) > 0.05 for wall, media in zip(wall_elapsed, x)):
        img, draw = new_canvas("Media Clock vs Wall Clock")
        rect = plot_rect()
        draw_grid(draw, rect)
        axis_max = nice_axis_max(max(wall_elapsed + x + [1.0]))
        x_pos = x_positions(rect, x)
        media_points = [(xpos, y_position(rect, x[idx], axis_max)) for idx, xpos in enumerate(x_pos)]
        wall_points = [(xpos, y_position(rect, wall_elapsed[idx], axis_max)) for idx, xpos in enumerate(x_pos)]
        draw_simple_line(draw, media_points, "#2A9D8F", width=3)
        draw_simple_line(draw, wall_points, "#BC6C25", width=3)
        draw_text(draw, (rect[0], rect[1] - 24), "Media", fill="#2A9D8F", font=small_font)
        draw_text(draw, (rect[0] + 110, rect[1] - 24), "Wall", fill="#BC6C25", font=small_font)
        save_chart(img, "media_vs_wall.png")

    summary = report_data.get("summary", {})
    received_frames = float(summary.get("frames_received", 0.0) or 0.0)
    dropped_frames = float(summary.get("estimated_dropped_frames", 0.0) or 0.0)
    total_frames = max(1.0, received_frames + dropped_frames)
    if received_frames <= 0 and dropped_frames <= 0:
        received_frames = 1.0
        dropped_frames = 0.0
    img, draw = new_canvas("Frame Distribution", width=820, height=820)
    pie_bounds = (120, 140, 700, 720)
    start_angle = -90.0
    received_angle = 360.0 * (received_frames / total_frames)
    drop_angle = 360.0 * (dropped_frames / total_frames)
    draw.pieslice(pie_bounds, start=start_angle, end=start_angle + received_angle, fill="#2A9D8F", outline="#FFFFFF")
    draw.pieslice(pie_bounds, start=start_angle + received_angle, end=start_angle + received_angle + drop_angle, fill="#E63946", outline="#FFFFFF")
    draw.rectangle((80, 90, 110, 120), fill="#2A9D8F")
    draw_text(draw, (126, 91), f"Received {received_frames:.0f}", fill="#102235", font=body_font)
    draw.rectangle((80, 128, 110, 158), fill="#E63946")
    draw_text(draw, (126, 129), f"Dropped {dropped_frames:.0f}", fill="#102235", font=body_font)
    save_chart(img, "frame_distribution.png")

    warning_breakdown = report_data.get("deep_diagnostics", {}).get("warnings_breakdown", {})
    if warning_breakdown:
        img, draw = new_canvas("Warning Categories", width=1200, height=620)
        rect = (90, 120, 1110, 520)
        draw_grid(draw, rect)
        keys = list(warning_breakdown.keys())
        vals = [float(warning_breakdown.get(key, 0) or 0) for key in keys]
        count_max = nice_axis_max(max(vals + [1.0]))
        bar_space = (rect[2] - rect[0]) / max(1, len(keys))
        for idx, key in enumerate(keys):
            left = int(rect[0] + idx * bar_space + 14)
            right = int(rect[0] + (idx + 1) * bar_space - 14)
            top = y_position(rect, vals[idx], count_max)
            draw.rectangle((left, top, right, rect[3]), fill="#F4A261")
            draw_text(draw, (left, rect[3] + 8), key, fill="#102235", font=tiny_font)
        save_chart(img, "warning_categories.png")

    return out


def generate_report_charts(report_data: dict, charts_dir: Path) -> dict[str, str]:
    if plt is None:
        return generate_report_charts_pillow(report_data, charts_dir)

    timeline = report_data.get("timeline", [])
    if not timeline:
        return {}
    try:
        charts_dir.mkdir(parents=True, exist_ok=True)
        out: dict[str, str] = {}

        x = [float(item.get("analysis_elapsed_sec", item.get("elapsed_sec", 0.0)) or 0.0) for item in timeline]
        frames = [float(item.get("frame", 0.0) or 0.0) for item in timeline]
        expected_frames = [float(item.get("expected_frames", 0.0) or 0.0) for item in timeline]
        drops = [float(item.get("estimated_drop_frames", 0.0) or 0.0) for item in timeline]
        drop_rate = [float(item.get("drop_rate_percent", 0.0) or 0.0) for item in timeline]
        bandwidth = [float(item.get("bandwidth_kbps_current", 0.0) or 0.0) for item in timeline]
        realtime_fps = [float(item.get("fps_realtime_num", 0.0) or 0.0) for item in timeline]
        health = [float(item.get("health_score", 0.0) or 0.0) for item in timeline]
        wall_elapsed = [float(item.get("wall_elapsed_sec", item.get("analysis_elapsed_sec", item.get("elapsed_sec", 0.0))) or 0.0) for item in timeline]

        plot_indices = select_chart_indices(len(x), 44)
        x_plot = [x[index] for index in plot_indices]
        frames_plot = [frames[index] for index in plot_indices]
        expected_frames_plot = [expected_frames[index] for index in plot_indices]
        drops_plot = [drops[index] for index in plot_indices]
        drop_rate_plot = [drop_rate[index] for index in plot_indices]
        bandwidth_plot = [bandwidth[index] for index in plot_indices]
        received_interval_plot = cumulative_to_interval(frames_plot)
        dropped_interval_plot = cumulative_to_interval(drops_plot)

        # Combined chart: interval frame bars + live bandwidth line.
        fig1, ax1 = plt.subplots(figsize=(10.8, 5.2), facecolor="#071523")
        ax1.set_facecolor("#071523")
        if len(x_plot) > 1:
            gaps = [curr - prev for prev, curr in zip(x_plot, x_plot[1:]) if curr > prev]
            min_gap = min(gaps) if gaps else 1.0
            bar_width = max(0.18, min(min_gap * 0.34, 1.25))
        else:
            bar_width = 0.55

        received_bars = ax1.bar(
            [value - (bar_width * 0.52) for value in x_plot],
            received_interval_plot,
            width=bar_width,
            color="#80ED99",
            alpha=0.88,
            label="Frames Received",
        )
        dropped_bars = ax1.bar(
            [value + (bar_width * 0.52) for value in x_plot],
            dropped_interval_plot,
            width=bar_width,
            color="#F94144",
            alpha=0.82,
            label="Dropped Frames",
        )
        avg_received = safe_mean([v for v in received_interval_plot if v > 0])
        if avg_received > 0:
            ax1.axhline(avg_received, color="#80ED99", linewidth=1.0, linestyle=":", alpha=0.55)
            ax1.text(
                x_plot[-1] if x_plot else 0,
                avg_received,
                f" avg {avg_received:.1f}",
                color="#80ED99",
                fontsize=7,
                va="bottom",
                alpha=0.85,
            )
        ax1.set_title("Live Dashboard  —  Frames Received vs Dropped + Bandwidth", color="#F8FAFC", fontsize=11, pad=6)
        ax1.set_xlabel("Elapsed Time (sec)", color="#E0E1DD")
        ax1.set_ylabel("Frames per Sample Interval", color="#E0E1DD")
        ax1.grid(True, axis="y", alpha=0.20, color="#37506B")
        ax1.tick_params(colors="#E0E1DD")
        for spine in ax1.spines.values():
            spine.set_color("#37506B")
        ax2 = ax1.twinx()
        ax2.set_facecolor("#071523")
        (bandwidth_line,) = ax2.plot(
            x_plot,
            bandwidth_plot,
            color="#4CC9F0",
            linewidth=2.3,
            marker="o",
            markersize=3.0,
            label="Bandwidth (kbps)",
        )
        avg_bw = safe_mean([v for v in bandwidth_plot if v > 0])
        if avg_bw > 0:
            ax2.axhline(avg_bw, color="#4CC9F0", linewidth=1.0, linestyle=":", alpha=0.50)
        ax2.set_ylabel("Bandwidth (kbps)", color="#E0E1DD")
        ax2.tick_params(colors="#E0E1DD")
        for spine in ax2.spines.values():
            spine.set_color("#37506B")
        ax1.legend(
            [received_bars, dropped_bars, bandwidth_line],
            ["Frames Received", "Dropped Frames", "Bandwidth (kbps)"],
            loc="center left",
            bbox_to_anchor=(1.08, 0.5),
            fontsize=8,
            title="Legend",
            frameon=True,
            facecolor="#102235",
            edgecolor="#37506B",
            labelcolor="#E0E1DD",
        )
        legend = ax1.get_legend()
        if legend:
            legend.get_title().set_color("#E0E1DD")
        fig1.text(
            0.5, 0.01,
            "Green bars = frames captured each interval  |  Red bars = frames dropped  |  Blue line = network bandwidth",
            ha="center", fontsize=7, color="#A9BCD0",
        )
        frames_chart = charts_dir / "timeline_frames.png"
        fig1.tight_layout(rect=(0, 0.04, 0.84, 1))
        fig1.savefig(frames_chart, dpi=150)
        plt.close(fig1)
        out["timeline_frames"] = str(frames_chart)

        fig_expected, ax_expected = plt.subplots(figsize=(10.4, 5.0))
        ax_expected.plot(x_plot, frames_plot, color="#2A9D8F", linewidth=2.3, label="Frames Received")
        ax_expected.plot(x_plot, expected_frames_plot, color="#264653", linewidth=2.0, linestyle="--", label="Expected Frames")
        ax_expected.fill_between(
            x_plot,
            frames_plot,
            expected_frames_plot,
            where=[exp >= frm for exp, frm in zip(expected_frames_plot, frames_plot)],
            color="#E76F51",
            alpha=0.28,
            interpolate=True,
            label="Drop Gap",
        )
        final_received = frames_plot[-1] if frames_plot else 0
        final_expected = expected_frames_plot[-1] if expected_frames_plot else 0
        gap = max(0.0, final_expected - final_received)
        gap_pct = (gap / final_expected * 100.0) if final_expected > 0 else 0.0
        ax_expected.set_title(
            f"Expected vs Received Frames  —  Final gap: {gap:.0f} frames ({gap_pct:.1f}%)",
            fontsize=11, pad=6,
        )
        ax_expected.set_xlabel("Elapsed Time (sec)")
        ax_expected.set_ylabel("Cumulative Frames")
        ax_expected.grid(True, alpha=0.28)
        ax_expected.legend(loc="upper left", fontsize=9)
        fig_expected.text(
            0.5, 0.01,
            "Shaded orange area = cumulative frame gap (expected minus received). Larger gap = more drops.",
            ha="center", fontsize=7.5, color="#555",
        )
        expected_chart = charts_dir / "expected_vs_received.png"
        fig_expected.tight_layout(rect=(0, 0.04, 1, 1))
        fig_expected.savefig(expected_chart, dpi=150)
        plt.close(fig_expected)
        out["expected_vs_received"] = str(expected_chart)

        fig2, (ax_mid, ax_bot) = plt.subplots(2, 1, figsize=(10, 6.0), sharex=True)
        avg_fps = safe_mean([v for v in realtime_fps if v > 0])
        ax_mid.plot(x, realtime_fps, color="#2A9D8F", linewidth=1.8, label="Realtime FPS")
        if avg_fps > 0:
            ax_mid.axhline(avg_fps, color="#2A9D8F", linewidth=1.0, linestyle="--", alpha=0.60, label=f"Avg {avg_fps:.1f} FPS")
        ax_mid.set_ylabel("Realtime FPS")
        ax_mid.set_title("Quality Timeline  —  FPS & Health Score over Time", pad=6)
        ax_mid.legend(loc="upper right", fontsize=8)
        ax_mid.grid(True, alpha=0.25)

        # Health zone bands: green >= 90, yellow 75-90, orange 55-75, red < 55
        ax_bot.axhspan(90, 100, color="#2dc653", alpha=0.10, label="Excellent (90-100)")
        ax_bot.axhspan(75, 90, color="#80c900", alpha=0.10, label="Good (75-89)")
        ax_bot.axhspan(55, 75, color="#f5a623", alpha=0.10, label="Fair (55-74)")
        ax_bot.axhspan(0, 55, color="#e63946", alpha=0.10, label="Poor (<55)")
        ax_bot.plot(x, health, color="#6A4C93", linewidth=2.0, zorder=3, label="Health Score")
        ax_bot.fill_between(x, health, alpha=0.15, color="#6A4C93")
        ax_bot.axhline(90, color="#2dc653", linewidth=0.8, linestyle=":", alpha=0.7)
        ax_bot.axhline(75, color="#80c900", linewidth=0.8, linestyle=":", alpha=0.7)
        ax_bot.axhline(55, color="#e63946", linewidth=0.8, linestyle=":", alpha=0.7)
        ax_bot.set_ylabel("Health Score (0–100)")
        ax_bot.set_xlabel("Elapsed Time (sec)")
        ax_bot.set_ylim(0, 105)
        ax_bot.grid(True, alpha=0.20)
        ax_bot.legend(loc="lower right", fontsize=7, ncol=2)
        fig2.text(
            0.5, 0.01,
            "Green zone = Excellent  |  Yellow = Good  |  Orange = Fair  |  Red = Poor",
            ha="center", fontsize=7.5, color="#555",
        )
        perf_chart = charts_dir / "timeline_performance.png"
        fig2.tight_layout(rect=(0, 0.04, 1, 1))
        fig2.savefig(perf_chart, dpi=150)
        plt.close(fig2)
        out["timeline_performance"] = str(perf_chart)

        fig_drop, (ax_drop_top, ax_drop_bottom) = plt.subplots(2, 1, figsize=(10.2, 5.8), sharex=True)
        ax_drop_top.plot(x, drops, color="#E63946", linewidth=2.2)
        ax_drop_top.fill_between(x, drops, color="#E63946", alpha=0.16)
        final_drops = drops[-1] if drops else 0
        ax_drop_top.set_ylabel("Cumulative Drops")
        ax_drop_top.set_title(f"Drop Timeline  —  Total dropped: {final_drops:.0f} frames", pad=6)
        ax_drop_top.grid(True, alpha=0.25)
        ax_drop_bottom.bar(x_plot, dropped_interval_plot, color="#F94144", alpha=0.75, label="Drops / Interval")
        ax_drop_bottom.plot(
            x_plot, drop_rate_plot, color="#6D597A", linewidth=1.8,
            marker="o", markersize=3.0, label="Drop Rate %",
        )
        avg_drop_rate = safe_mean([v for v in drop_rate_plot if v > 0])
        if avg_drop_rate > 0:
            ax_drop_bottom.axhline(avg_drop_rate, color="#6D597A", linewidth=1.0, linestyle="--", alpha=0.60,
                                   label=f"Avg {avg_drop_rate:.1f}%")
        ax_drop_bottom.set_ylabel("Drop Count / Rate (%)")
        ax_drop_bottom.set_xlabel("Elapsed Time (sec)")
        ax_drop_bottom.grid(True, alpha=0.25)
        ax_drop_bottom.legend(loc="upper right", fontsize=8)
        fig_drop.text(
            0.5, 0.01,
            "Top panel: cumulative drops over time  |  Bottom: drops per sample + instantaneous drop rate %",
            ha="center", fontsize=7.5, color="#555",
        )
        drop_chart = charts_dir / "drop_timeline.png"
        fig_drop.tight_layout(rect=(0, 0.04, 1, 1))
        fig_drop.savefig(drop_chart, dpi=150)
        plt.close(fig_drop)
        out["drop_timeline"] = str(drop_chart)

        if any(value > 0 for value in bandwidth):
            fig_bw, ax_bw = plt.subplots(figsize=(9.6, 4.6))
            positive_bandwidth = [value for value in bandwidth if value > 0]
            ax_bw.hist(
                positive_bandwidth,
                bins=min(14, max(6, len(positive_bandwidth) // 2)),
                color="#4CC9F0",
                edgecolor="#264653",
                alpha=0.85,
            )
            bw_mean = safe_mean(positive_bandwidth)
            bw_med = safe_percentile(positive_bandwidth, 50.0)
            bw_p95 = safe_percentile(positive_bandwidth, 95.0)
            ax_bw.axvline(bw_mean, color="#F4A261", linewidth=2.0, linestyle="--", label=f"Mean: {bw_mean:.0f} kbps")
            ax_bw.axvline(bw_med, color="#2A9D8F", linewidth=1.8, linestyle=":", label=f"Median: {bw_med:.0f} kbps")
            ax_bw.axvline(bw_p95, color="#E63946", linewidth=1.5, linestyle="-.", label=f"P95: {bw_p95:.0f} kbps")
            ax_bw.set_title(f"Bandwidth Distribution  —  Mean {bw_mean:.0f} kbps  |  P95 {bw_p95:.0f} kbps", pad=6)
            ax_bw.set_xlabel("Bandwidth (kbps)")
            ax_bw.set_ylabel("Number of Samples")
            ax_bw.grid(True, axis="y", alpha=0.25)
            ax_bw.legend(loc="upper right", fontsize=8)
            fig_bw.text(
                0.5, 0.01,
                "Each bar = how many samples had that bandwidth.  Narrow spread = stable stream.",
                ha="center", fontsize=7.5, color="#555",
            )
            bandwidth_hist = charts_dir / "bandwidth_distribution.png"
            fig_bw.tight_layout(rect=(0, 0.04, 1, 1))
            fig_bw.savefig(bandwidth_hist, dpi=150)
            plt.close(fig_bw)
            out["bandwidth_distribution"] = str(bandwidth_hist)

        if any(abs(wall - media) > 0.05 for wall, media in zip(wall_elapsed, x)):
            fig_clock, ax_clock = plt.subplots(figsize=(10.0, 4.6))
            ax_clock.plot(x, x, color="#2A9D8F", linewidth=2.0, label="Media Clock (ideal)")
            ax_clock.plot(x, wall_elapsed, color="#BC6C25", linewidth=2.0, linestyle="--", label="Wall Clock (actual)")
            ax_clock.fill_between(x, x, wall_elapsed, color="#BC6C25", alpha=0.18, label="Clock drift")
            drift_values = [abs(w - m) for w, m in zip(wall_elapsed, x)]
            max_drift = max(drift_values) if drift_values else 0.0
            ax_clock.set_title(f"Media Clock vs Wall Clock  —  Max drift: {max_drift:.2f}s", pad=6)
            ax_clock.set_xlabel("Timeline Sample (sec)")
            ax_clock.set_ylabel("Elapsed Time (sec)")
            ax_clock.grid(True, alpha=0.25)
            ax_clock.legend(loc="upper left", fontsize=8)
            fig_clock.text(
                0.5, 0.01,
                "Gap between lines = clock drift.  Large drift may indicate buffering, re-buffering, or time-sync issues.",
                ha="center", fontsize=7.5, color="#555",
            )
            clock_chart = charts_dir / "media_vs_wall.png"
            fig_clock.tight_layout(rect=(0, 0.04, 1, 1))
            fig_clock.savefig(clock_chart, dpi=150)
            plt.close(fig_clock)
            out["media_vs_wall"] = str(clock_chart)

        summary = report_data.get("summary", {})
        received_frames = float(summary.get("frames_received", 0.0) or 0.0)
        dropped_frames = float(summary.get("estimated_dropped_frames", 0.0) or 0.0)
        if received_frames <= 0 and dropped_frames <= 0:
            received_frames = 1.0
            dropped_frames = 0.0
        total_f = received_frames + max(0.0, dropped_frames)
        fig3, ax3 = plt.subplots(figsize=(5.6, 5.6))
        wedge_props = {"linewidth": 1.5, "edgecolor": "#ffffff"}
        wedges, texts, autotexts = ax3.pie(
            [received_frames, max(0.0, dropped_frames)],
            labels=[f"Received\n{received_frames:.0f}", f"Dropped\n{dropped_frames:.0f}"],
            colors=["#2A9D8F", "#E63946"],
            autopct="%1.1f%%",
            startangle=90,
            wedgeprops=wedge_props,
            textprops={"fontsize": 10},
        )
        for autotext in autotexts:
            autotext.set_fontsize(9)
            autotext.set_fontweight("bold")
        ax3.set_title(f"Frame Distribution  —  {total_f:.0f} total frames", pad=8)
        pie_chart = charts_dir / "frame_distribution_pie.png"
        fig3.tight_layout()
        fig3.savefig(pie_chart, dpi=160)
        plt.close(fig3)
        out["frame_distribution"] = str(pie_chart)

        warning_breakdown = report_data.get("deep_diagnostics", {}).get("warnings_breakdown", {})
        if warning_breakdown:
            keys = list(warning_breakdown.keys())
            vals = [float(warning_breakdown.get(k, 0) or 0) for k in keys]
            bar_colors = ["#E76F51" if v >= max(vals) * 0.7 else "#F4A261" for v in vals]
            fig4, ax4 = plt.subplots(figsize=(8.4, 4.0))
            bars4 = ax4.bar(keys, vals, color=bar_colors, edgecolor="#264653", linewidth=0.8)
            for bar, val in zip(bars4, vals):
                if val > 0:
                    ax4.text(
                        bar.get_x() + bar.get_width() / 2.0,
                        bar.get_height() + 0.05,
                        str(int(val)),
                        ha="center", va="bottom", fontsize=9, fontweight="bold",
                    )
            ax4.set_title("Warning Categories  —  by type", pad=6)
            ax4.set_ylabel("Warning Count")
            ax4.grid(True, axis="y", alpha=0.25)
            fig4.text(
                0.5, 0.01,
                "Darker bars = most frequent warning type.  Timeout & packet_loss warnings suggest network instability.",
                ha="center", fontsize=7.5, color="#555",
            )
            warn_chart = charts_dir / "warning_categories.png"
            fig4.tight_layout(rect=(0, 0.06, 1, 1))
            fig4.savefig(warn_chart, dpi=150)
            plt.close(fig4)
            out["warning_categories"] = str(warn_chart)

        return out
    except Exception:
        return generate_report_charts_pillow(report_data, charts_dir)


def get_app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(relative_path: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base_dir = Path(getattr(sys, "_MEIPASS"))
    else:
        base_dir = Path(__file__).resolve().parent
    return base_dir / relative_path


def find_binary_in_root(root_dir: Path, exe_name: str) -> Optional[str]:
    if not root_dir.exists():
        return None

    direct_candidates = [
        root_dir / exe_name,
        root_dir / "bin" / exe_name,
    ]
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate)

    try:
        discovered = list(root_dir.rglob(exe_name))
    except Exception:
        return None
    if not discovered:
        return None

    def _rank(path_obj: Path) -> tuple[int, int]:
        parts = [part.lower() for part in path_obj.parts]
        in_bin = 0 if "bin" in parts else 1
        depth = len(parts)
        return (in_bin, depth)

    discovered.sort(key=_rank)
    return str(discovered[0])


def normalize_rtsp_url(rtsp_url: str) -> str:
    raw = (rtsp_url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    if parsed.scheme.lower() != "rtsp" or not parsed.netloc or "@" not in parsed.netloc:
        return raw
    userinfo, host = parsed.netloc.rsplit("@", 1)
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
        encoded_user = quote(unquote(user), safe="")
        encoded_pwd = quote(unquote(pwd), safe="")
        safe_netloc = f"{encoded_user}:{encoded_pwd}@{host}"
    else:
        safe_netloc = f"{quote(unquote(userinfo), safe='')}@{host}"
    return urlunsplit((parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment))


def shorten_text(value: str, max_len: int = 90) -> str:
    if len(value) <= max_len:
        return value
    return f"{value[:max_len-3]}..."


def parse_frame_rate(rate_text: str) -> float:
    if not rate_text or rate_text in {"0/0", "N/A"}:
        return 0.0
    if "/" in rate_text:
        num_text, den_text = rate_text.split("/", 1)
        try:
            num = float(num_text)
            den = float(den_text)
            if den == 0:
                return 0.0
            return num / den
        except ValueError:
            return 0.0
    try:
        return float(rate_text)
    except ValueError:
        return 0.0


def normalize_transport_mode(value: str) -> str:
    mode = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "multicast": "udp_multicast",
        "udp_multicast": "udp_multicast",
        "udp_multicast_rtp": "udp_multicast",
        "udp_unicast": "udp",
        "unicast": "udp",
    }
    mode = aliases.get(mode, mode)
    if mode in RTSP_TRANSPORT_MODES:
        return mode
    return "auto"


def resolve_transport_for_ff_tools(value: str) -> str:
    mode = normalize_transport_mode(value)
    if mode in {"udp", "udp_multicast"}:
        return mode
    return "tcp"


def transport_delivery_label(mode: str) -> str:
    normalized = normalize_transport_mode(mode)
    if normalized == "udp_multicast":
        return "multicast"
    if normalized in {"tcp", "udp"}:
        return "unicast"
    return "auto"


def normalize_engine_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode in DIAGNOSTIC_ENGINES:
        return mode
    return "ffmpeg"


def hhmmss_to_seconds(value: str) -> float:
    if not value or value == "N/A":
        return 0.0
    parts = value.split(":")
    if len(parts) != 3:
        return 0.0
    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return 0.0
    return hours * 3600 + minutes * 60 + seconds


def detect_ff_binary(binary_name: str) -> Optional[str]:
    exe_name = f"{binary_name}.exe"
    base_dir = get_app_base_dir()
    preferred_candidates = [
        base_dir / exe_name,
        base_dir / "ffmpeg" / "bin" / exe_name,
        Path("C:/ffmpeg") / exe_name,
        Path("C:/FFMPEG") / exe_name,
        Path("C:/FFMPEG/bin") / exe_name,
        Path("C:/ffmpeg/bin") / exe_name,
        Path("C:/Program Files/ffmpeg/bin") / exe_name,
        Path("C:/Program Files (x86)/ffmpeg/bin") / exe_name,
    ]
    for candidate in preferred_candidates:
        if candidate.exists():
            return str(candidate)

    for root in (Path("C:/ffmpeg"), Path("C:/FFMPEG")):
        found = find_binary_in_root(root, exe_name)
        if found:
            return found

    located = shutil.which(binary_name)
    if located:
        return located

    env_local_app_data = os.environ.get("LOCALAPPDATA")
    if env_local_app_data:
        winget_root = Path(env_local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            for package_dir in sorted(winget_root.glob("Gyan.FFmpeg*"), reverse=True):
                candidate_paths = list(package_dir.rglob(exe_name))
                if candidate_paths:
                    return str(candidate_paths[0])

    return None


def detect_gst_binary(binary_name: str) -> Optional[str]:
    exe_name = f"{binary_name}.exe"
    base_dir = get_app_base_dir()
    preferred_candidates = [
        base_dir / exe_name,
        base_dir / "gstreamer" / "1.0" / "msvc_x86_64" / "bin" / exe_name,
        base_dir / "gstreamer" / "1.0" / "mingw_x86_64" / "bin" / exe_name,
        base_dir / "runtime" / "gstreamer" / "1.0" / "msvc_x86_64" / "bin" / exe_name,
        base_dir / "runtime" / "gstreamer" / "1.0" / "mingw_x86_64" / "bin" / exe_name,
        Path("C:/gstreamer/1.0/msvc_x86_64/bin") / exe_name,
        Path("C:/gstreamer/1.0/mingw_x86_64/bin") / exe_name,
        Path("C:/Program Files/gstreamer/1.0/msvc_x86_64/bin") / exe_name,
        Path("C:/Program Files/gstreamer/1.0/mingw_x86_64/bin") / exe_name,
        Path("C:/Program Files (x86)/gstreamer/1.0/msvc_x86_64/bin") / exe_name,
        Path("C:/Program Files (x86)/gstreamer/1.0/mingw_x86_64/bin") / exe_name,
    ]
    env_local_app_data = os.environ.get("LOCALAPPDATA")
    if env_local_app_data:
        preferred_candidates.extend(
            [
                Path(env_local_app_data) / "Programs" / "gstreamer" / "1.0" / "msvc_x86_64" / "bin" / exe_name,
                Path(env_local_app_data) / "Programs" / "gstreamer" / "1.0" / "mingw_x86_64" / "bin" / exe_name,
            ]
        )
    for candidate in preferred_candidates:
        if candidate.exists():
            return str(candidate)

    located = shutil.which(binary_name)
    if located:
        return located
    return None


def build_gstreamer_env(gst_binary_path: Optional[str]) -> dict[str, str]:
    env = os.environ.copy()
    if not gst_binary_path:
        return env

    try:
        bin_dir = Path(gst_binary_path).resolve().parent
    except Exception:
        return env

    runtime_root = bin_dir.parent
    existing_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(part for part in (str(bin_dir), existing_path) if part)

    plugin_dir = runtime_root / "lib" / "gstreamer-1.0"
    if plugin_dir.exists():
        env["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(plugin_dir)
        env.setdefault("GST_PLUGIN_PATH_1_0", str(plugin_dir))

    typelib_dir = runtime_root / "lib" / "girepository-1.0"
    if typelib_dir.exists():
        env["GI_TYPELIB_PATH"] = str(typelib_dir)

    scanner_candidates = [
        runtime_root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe",
        runtime_root / "lib" / "gstreamer-1.0" / "gst-plugin-scanner.exe",
        bin_dir / "gst-plugin-scanner.exe",
    ]
    for candidate in scanner_candidates:
        if candidate.exists():
            env["GST_PLUGIN_SCANNER"] = str(candidate)
            break

    return env


def simplify_codec_name(value: str) -> str:
    token = re.sub(r"\s*\(.*?\)\s*", "", str(value or "")).strip().lower()
    aliases = {
        "h.265": "hevc",
        "h265": "hevc",
        "video/x-h265": "hevc",
        "h.264": "h264",
        "h264": "h264",
        "video/x-h264": "h264",
        "mpeg-4 aac": "aac",
        "audio/mpeg": "aac",
    }
    return aliases.get(token, token.replace(" ", "_") or "unknown")


def extract_gst_int(text: str, key: str) -> int:
    match = re.search(rf"{re.escape(key)}\\*=\s*\\*\((?:int|uint|guint64)\\*\)(-?\d+)", text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def extract_gst_fraction(text: str, key: str) -> str:
    match = re.search(rf"{re.escape(key)}\\*=\s*\\*\(fraction\\*\)(\d+/\d+)", text)
    if match:
        return match.group(1)
    match = re.search(rf"{re.escape(key)}\\*=\s*\\*\(string\\*\)([0-9]+(?:\.[0-9]+)?)", text)
    if match:
        value = match.group(1)
        if "." in value:
            return value
        return f"{value}/1"
    return "0/0"


def extract_gst_tag_value(text: str, key: str) -> str:
    match = re.search(rf"{re.escape(key)}:\s*(.+)$", text.strip(), re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def parse_gst_progress_seconds(line: str) -> Optional[float]:
    match = re.search(r"progressreport\d+\s+\(([^)]+)\):\s+([0-9]+(?:\.[0-9]+)?)\s+seconds", line)
    if not match:
        return None
    try:
        return float(match.group(2))
    except ValueError:
        return None


def parse_gst_bitrate_bps(line: str) -> Optional[int]:
    match = re.search(r"(?<![A-Za-z-])bitrate\\*=\s*\\*\((?:uint|guint64)\\*\)(\d+)", line)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_gst_packet_stats(line: str) -> dict[str, int]:
    return {
        "packets_lost": extract_gst_int(line, "packets-lost"),
        "recv_packet_rate": extract_gst_int(line, "recv-packet-rate"),
        "jitter": extract_gst_int(line, "jitter"),
    }


def parse_gst_discoverer_output(
    output_text: str,
    *,
    requested_transport: str,
    selected_transport: str,
    transport_tests: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    requested_transport = normalize_transport_mode(requested_transport)
    selected_transport = normalize_transport_mode(selected_transport)
    lines = output_text.splitlines()
    streams: list[dict[str, Any]] = []
    current_stream: Optional[dict[str, Any]] = None
    current_section = ""
    codec_tags: dict[str, str] = {}
    live = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        lowered = stripped.lower()
        if lowered.startswith("live:"):
            live = lowered.endswith("yes")
            continue
        if stripped == "Tags:":
            current_section = "tags"
            continue
        if stripped.endswith(":") and stripped != "Tags:":
            current_section = stripped[:-1].lower()
            continue

        stream_match = re.match(r"(audio|video)\s+#(\d+):\s*(.+)$", stripped, re.IGNORECASE)
        if stream_match:
            if current_stream:
                streams.append(current_stream)
            codec_type = stream_match.group(1).lower()
            index = int(stream_match.group(2))
            caps_text = stream_match.group(3)
            fps_raw = extract_gst_fraction(caps_text, "framerate")
            current_stream = {
                "index": index,
                "codec_type": codec_type,
                "codec_name": simplify_codec_name(caps_text.split(",", 1)[0]),
                "codec_display": codec_tags.get(codec_type, "").strip() or caps_text.split(",", 1)[0].strip(),
                "profile": "",
                "width": extract_gst_int(caps_text, "width"),
                "height": extract_gst_int(caps_text, "height"),
                "pix_fmt": "unknown",
                "avg_frame_rate_raw": fps_raw,
                "r_frame_rate_raw": fps_raw,
                "fps": round(parse_frame_rate(fps_raw), 3),
                "bit_rate_kbps": 0.0,
                "sample_rate_hz": extract_gst_int(caps_text, "rate"),
                "channels": extract_gst_int(caps_text, "channels"),
                "channel_layout": "",
                "caps": caps_text,
            }
            current_section = codec_type
            continue

        if current_section == "tags":
            if ":" in stripped:
                tag_name, tag_value = stripped.split(":", 1)
                tag_name = tag_name.strip().lower()
                tag_value = tag_value.strip()
                if tag_name == "video codec":
                    codec_tags["video"] = tag_value
                elif tag_name == "audio codec":
                    codec_tags["audio"] = tag_value
            continue

        if current_stream:
            if stripped.startswith("Bitrate:"):
                bitrate = parse_int_token(stripped)
                if bitrate > 0:
                    current_stream["bit_rate_kbps"] = round(bitrate / 1000.0, 3)
            elif stripped.startswith("Max bitrate:"):
                pass
            elif stripped.startswith("Width:"):
                current_stream["width"] = parse_int_token(stripped)
            elif stripped.startswith("Height:"):
                current_stream["height"] = parse_int_token(stripped)
            elif stripped.startswith("Channels:"):
                current_stream["channels"] = parse_int_token(stripped)
            elif stripped.startswith("Sample rate:"):
                current_stream["sample_rate_hz"] = parse_int_token(stripped)
            elif stripped.startswith("Frame rate:"):
                fps_raw = stripped.split(":", 1)[1].strip()
                current_stream["avg_frame_rate_raw"] = fps_raw
                current_stream["r_frame_rate_raw"] = fps_raw
                current_stream["fps"] = round(parse_frame_rate(fps_raw), 3)
            elif stripped.startswith("Codec:"):
                codec_blob = stripped.split(":", 1)[1].strip()
                current_stream["codec_name"] = simplify_codec_name(codec_blob.split(",", 1)[0])
                current_stream["codec_display"] = codec_tags.get(current_stream["codec_type"], "").strip() or codec_blob

    if current_stream:
        streams.append(current_stream)

    for item in streams:
        codec_type = str(item.get("codec_type", "unknown"))
        item["codec_display"] = codec_tags.get(codec_type, "").strip() or str(item.get("codec_display", ""))

    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    primary_video = video_streams[0] if video_streams else {}

    tests_payload: dict[str, dict[str, Any]] = {}
    for transport, result in transport_tests.items():
        tests_payload[transport] = {
            **result,
            "stream_count": len(streams) if result.get("ok") and transport == selected_transport else int(result.get("stream_count", 0) or 0),
        }

    return {
        "requested_transport": requested_transport,
        "selected_transport": selected_transport,
        "requested_delivery": transport_delivery_label(requested_transport),
        "selected_delivery": transport_delivery_label(selected_transport),
        "transport_diagnostics": {
            "requested": requested_transport,
            "selected": selected_transport,
            "tests": tests_payload,
        },
        "stream_count": len(streams),
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(audio_streams),
        "first_video_index": int(primary_video.get("index", 0) or 0),
        "format_bit_rate_bps": 0,
        "format_bit_rate_kbps": 0.0,
        "streams": streams,
        "codec_name": primary_video.get("codec_name", "unknown"),
        "codec_display": primary_video.get("codec_display", ""),
        "width": primary_video.get("width", 0),
        "height": primary_video.get("height", 0),
        "pix_fmt": primary_video.get("pix_fmt", "unknown"),
        "avg_frame_rate_raw": primary_video.get("avg_frame_rate_raw", "0/0"),
        "r_frame_rate_raw": primary_video.get("r_frame_rate_raw", "0/0"),
        "fps": round(float(primary_video.get("fps", 0.0) or 0.0), 3),
        "bit_rate_kbps": float(primary_video.get("bit_rate_kbps", 0.0) or 0.0),
        "is_live": live,
        "discovery_backend": "gst-discoverer-1.0",
    }


def run_gst_discoverer_query(
    *,
    gst_discoverer_path: str,
    gst_launch_path: Optional[str],
    rtsp_url: str,
    timeout_sec: int = 20,
) -> tuple[bool, str, str]:
    gst_env = build_gstreamer_env(gst_launch_path or gst_discoverer_path)
    cmd = [gst_discoverer_path, "-v", "-t", str(timeout_sec), rtsp_url]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(timeout_sec + 4, 8),
            encoding="utf-8",
            errors="replace",
            check=False,
            env=gst_env,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False, "", "gst-discoverer timeout"
    except Exception as exc:
        return False, "", f"gst-discoverer error: {exc}"

    combined = "\n".join(part for part in ((result.stdout or "").strip(), (result.stderr or "").strip()) if part)
    if result.returncode != 0:
        return False, combined, combined or f"gst-discoverer failed with code {result.returncode}"
    if "Done discovering" not in combined:
        return False, combined, "gst-discoverer returned no stream details"
    return True, combined, ""


def probe_stream_gstreamer(
    *,
    gst_launch_path: Optional[str],
    gst_play_path: Optional[str],
    gst_discoverer_path: Optional[str],
    rtsp_url: str,
    requested_transport: str = "auto",
) -> dict[str, Any]:
    request_mode = normalize_transport_mode(requested_transport)
    probe_order = list(RTSP_TRANSPORT_PROBE_CANDIDATES) if request_mode == "auto" else [request_mode]
    background_order = [t for t in RTSP_TRANSPORT_PROBE_CANDIDATES if t not in probe_order]
    full_order = probe_order + background_order

    transport_tests: dict[str, dict[str, Any]] = {}
    selected_transport = ""
    for transport in full_order:
        ok, payload, err = run_gstreamer_probe(
            gst_launch_path=gst_launch_path,
            gst_play_path=gst_play_path,
            rtsp_url=rtsp_url,
            transport=transport,
            timeout_sec=6,
        )
        transport_tests[transport] = {
            **payload,
            "ok": ok,
            "error": err,
        }
        if ok and not selected_transport and transport in probe_order:
            selected_transport = transport

    if not selected_transport:
        raise RuntimeError("GStreamer could not open the RTSP stream using the requested transport modes.")

    if gst_discoverer_path:
        ok, discoverer_output, err = run_gst_discoverer_query(
            gst_discoverer_path=gst_discoverer_path,
            gst_launch_path=gst_launch_path,
            rtsp_url=rtsp_url,
            timeout_sec=12,
        )
        if ok:
            return parse_gst_discoverer_output(
                discoverer_output,
                requested_transport=requested_transport,
                selected_transport=selected_transport,
                transport_tests=transport_tests,
            )
        transport_tests[selected_transport]["discoverer_error"] = err

    return {
        "requested_transport": request_mode,
        "selected_transport": selected_transport,
        "requested_delivery": transport_delivery_label(request_mode),
        "selected_delivery": transport_delivery_label(selected_transport),
        "transport_diagnostics": {
            "requested": request_mode,
            "selected": selected_transport,
            "tests": transport_tests,
        },
        "stream_count": 0,
        "video_stream_count": 0,
        "audio_stream_count": 0,
        "first_video_index": 0,
        "format_bit_rate_bps": 0,
        "format_bit_rate_kbps": 0.0,
        "streams": [],
        "codec_name": "unknown",
        "codec_display": "",
        "width": 0,
        "height": 0,
        "pix_fmt": "unknown",
        "avg_frame_rate_raw": "0/0",
        "r_frame_rate_raw": "0/0",
        "fps": 0.0,
        "bit_rate_kbps": 0.0,
        "is_live": True,
        "discovery_backend": "gst-launch-1.0 (probe only)",
    }


def run_gstreamer_probe(
    *,
    gst_launch_path: Optional[str],
    gst_play_path: Optional[str],
    rtsp_url: str,
    transport: str,
    timeout_sec: int = 6,
) -> tuple[bool, dict[str, Any], str]:
    gst_binary = gst_launch_path or gst_play_path
    if not gst_binary:
        return False, {}, "No GStreamer runtime was detected."

    gst_env = build_gstreamer_env(gst_binary)
    gst_protocol = {
        "tcp": "tcp",
        "udp": "udp",
        "udp_multicast": "udp-mcast",
    }.get(transport, "tcp")

    if gst_launch_path:
        probe_cmd = [
            gst_launch_path,
            "-q",
            "playbin",
            f"uri={rtsp_url}",
            f"source::protocols={gst_protocol}",
            "source::latency=0",
            "video-sink=fakesink",
            "audio-sink=fakesink",
        ]
    else:
        probe_cmd = [gst_play_path, "--no-interactive", rtsp_url]

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            probe_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=gst_env,
            **hidden_subprocess_kwargs(),
        )
    except Exception as exc:
        return False, {}, f"Unable to launch GStreamer: {exc}"

    stderr_text = ""
    ok = False
    try:
        time.sleep(timeout_sec)
        ok = proc.poll() is None or proc.returncode == 0
        if proc.stderr and proc.poll() is not None:
            stderr_text = proc.stderr.read().strip()
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    result = {
        "ok": bool(ok),
        "binary": str(gst_binary),
        "transport": transport,
        "startup_check_sec": round(time.monotonic() - started, 3),
        "command": " ".join(str(part) for part in probe_cmd),
    }
    if ok:
        return True, result, ""
    return False, result, stderr_text or "GStreamer probe failed."


def run_ffprobe_stream_query(
    *,
    ffprobe_path: str,
    rtsp_url: str,
    transport: str,
    timeout_sec: int = 20,
) -> tuple[bool, dict[str, Any], str]:
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-rtsp_transport",
        transport,
        "-show_streams",
        "-show_format",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,profile,width,height,pix_fmt,"
            "avg_frame_rate,r_frame_rate,bit_rate,sample_rate,channels,channel_layout"
        ),
        "-show_entries",
        "format=bit_rate",
        "-of",
        "json",
        rtsp_url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False, {}, f"ffprobe timeout using {transport.upper()}"
    except Exception as exc:
        return False, {}, f"ffprobe error using {transport.upper()}: {exc}"

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        return False, {}, details or f"ffprobe failed using {transport.upper()}"

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return False, {}, f"ffprobe returned invalid JSON using {transport.upper()}: {exc}"

    streams = payload.get("streams", [])
    if not streams:
        return False, payload, f"No streams detected using {transport.upper()}"
    return True, payload, ""


def probe_stream(ffprobe_path: str, rtsp_url: str, requested_transport: str = "auto") -> dict:
    request_mode = normalize_transport_mode(requested_transport)
    probe_order = list(RTSP_TRANSPORT_PROBE_CANDIDATES) if request_mode == "auto" else [request_mode]
    # Keep cross-transport diagnostics in report without forcing startup to fail
    # when the non-selected transport is unavailable.
    background_order = [t for t in RTSP_TRANSPORT_PROBE_CANDIDATES if t not in probe_order]
    full_order = probe_order + background_order

    transport_tests: dict[str, dict[str, Any]] = {}
    payload_by_transport: dict[str, dict[str, Any]] = {}
    for transport in full_order:
        ok, payload, err = run_ffprobe_stream_query(
            ffprobe_path=ffprobe_path,
            rtsp_url=rtsp_url,
            transport=transport,
            timeout_sec=15,
        )
        stream_count = len(payload.get("streams", [])) if payload else 0
        transport_tests[transport] = {
            "ok": bool(ok),
            "stream_count": int(stream_count),
            "error": str(err),
        }
        if ok:
            payload_by_transport[transport] = payload

    selected_transport = ""
    if request_mode == "auto":
        for candidate in RTSP_TRANSPORT_PROBE_CANDIDATES:
            if transport_tests.get(candidate, {}).get("ok"):
                selected_transport = candidate
                break
    else:
        if transport_tests.get(request_mode, {}).get("ok"):
            selected_transport = request_mode

    if not selected_transport:
        errors = [
            f"{mode.upper()}: {transport_tests.get(mode, {}).get('error', 'n/a') or 'n/a'}"
            for mode in RTSP_TRANSPORT_PROBE_CANDIDATES
        ]
        raise RuntimeError(
            "RTSP probe failed for all transport modes. "
            + " | ".join(errors)
        )

    payload = payload_by_transport[selected_transport]
    streams_raw = payload.get("streams", []) or []
    streams: list[dict[str, Any]] = []
    for raw in streams_raw:
        codec_type = str(raw.get("codec_type", "") or "")
        avg_rate = str(raw.get("avg_frame_rate", "0/0") or "0/0")
        real_rate = str(raw.get("r_frame_rate", "0/0") or "0/0")
        fps = parse_frame_rate(avg_rate) or parse_frame_rate(real_rate)
        bit_rate_bps = parse_int_token(str(raw.get("bit_rate", "0")))
        stream_item = {
            "index": int(raw.get("index", 0) or 0),
            "codec_type": codec_type,
            "codec_name": str(raw.get("codec_name", "unknown") or "unknown"),
            "profile": str(raw.get("profile", "") or ""),
            "width": int(raw.get("width", 0) or 0),
            "height": int(raw.get("height", 0) or 0),
            "pix_fmt": str(raw.get("pix_fmt", "unknown") or "unknown"),
            "avg_frame_rate_raw": avg_rate,
            "r_frame_rate_raw": real_rate,
            "fps": round(fps, 3),
            "bit_rate_bps": int(bit_rate_bps),
            "bit_rate_kbps": round(bit_rate_bps / 1000.0, 3) if bit_rate_bps > 0 else 0.0,
            "sample_rate_hz": parse_int_token(str(raw.get("sample_rate", "0"))),
            "channels": parse_int_token(str(raw.get("channels", "0"))),
            "channel_layout": str(raw.get("channel_layout", "") or ""),
        }
        streams.append(stream_item)

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise RuntimeError("No video stream detected in RTSP source.")

    primary_video = video_streams[0]
    format_info = payload.get("format", {}) or {}
    format_bit_rate_bps = parse_int_token(str(format_info.get("bit_rate", "0")))

    return {
        "selected_transport": selected_transport,
        "requested_transport": request_mode,
        "selected_delivery": transport_delivery_label(selected_transport),
        "requested_delivery": transport_delivery_label(request_mode),
        "transport_diagnostics": {
            "requested": request_mode,
            "selected": selected_transport,
            "tests": transport_tests,
        },
        "stream_count": len(streams),
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(audio_streams),
        "first_video_index": int(primary_video.get("index", 0) or 0),
        "format_bit_rate_bps": int(format_bit_rate_bps),
        "format_bit_rate_kbps": round(format_bit_rate_bps / 1000.0, 3) if format_bit_rate_bps > 0 else 0.0,
        "streams": streams,
        "codec_name": primary_video.get("codec_name", "unknown"),
        "width": primary_video.get("width", 0),
        "height": primary_video.get("height", 0),
        "pix_fmt": primary_video.get("pix_fmt", "unknown"),
        "avg_frame_rate_raw": primary_video.get("avg_frame_rate_raw", "0/0"),
        "r_frame_rate_raw": primary_video.get("r_frame_rate_raw", "0/0"),
        "fps": round(float(primary_video.get("fps", 0.0) or 0.0), 3),
        "bit_rate_kbps": float(primary_video.get("bit_rate_kbps", 0.0) or 0.0),
    }


def should_count_warning(log_line: str) -> bool:
    warning_patterns = [
        r"\berror\b",
        r"\bcorrupt\b",
        r"\bmissed\b",
        r"\btimed out\b",
        r"\binvalid\b",
        r"\bdecode\b",
        r"\boverrun\b",
        r"\bnon-existing\b",
        r"\bfailed\b",
    ]
    lowered = log_line.lower()
    return any(re.search(pattern, lowered) for pattern in warning_patterns)


@dataclass
class RunContext:
    rtsp_url: str
    duration_seconds: int
    engine_mode: str
    transport_mode: str
    ffmpeg_path: Optional[str]
    ffprobe_path: Optional[str]
    gst_launch_path: Optional[str]
    gst_play_path: Optional[str]
    gst_discoverer_path: Optional[str]
    output_dir: str
    run_id: str


class DiagnosticWorker(threading.Thread):
    def __init__(self, context: RunContext, event_queue: queue.Queue):
        super().__init__(daemon=True)
        self.context = context
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.process: Optional[subprocess.Popen] = None
        self.stream_info: dict = {}
        self.last_progress: dict = {}
        self.warning_count = 0
        self.warning_samples: list[str] = []
        self.warning_breakdown: dict[str, int] = {}
        self.rtp_missed_packets = 0
        self.started_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        self.monotonic_started = 0.0
        self.first_media_progress_wall: Optional[float] = None
        self.first_frame_media_ts: Optional[float] = None
        self.last_elapsed_sec: Optional[float] = None
        self.last_frame_count: Optional[int] = None
        self.frame_rate_samples: list[float] = []
        self.realtime_fps_samples: list[float] = []
        self.bitrate_kbps_samples: list[float] = []
        self.speed_samples: list[float] = []
        self.freeze_start_media_ts: Optional[float] = None
        self.freeze_events: list[dict] = []
        self.timeline_samples: list[dict] = []
        self.snapshot_path = ""
        self.snapshot_error = ""
        self.fallback_nominal_fps = 0.0
        self.last_total_size_bytes = 0
        self.drop_baseline_bias = 0.0
        self.drop_baseline_set = False
        self.gstreamer_probe_result: dict[str, Any] = {}
        self.gstreamer_runtime_details: dict[str, Any] = {}
        self.last_wall_sample_sec: Optional[float] = None
        self.latest_gstreamer_bitrate_kbps = 0.0
        self.latest_gstreamer_audio_bitrate_kbps = 0.0
        self.duration_complete = False
        self.gstreamer_drop_equivalent_frames = 0.0
        self.gstreamer_wall_drift_samples: list[float] = []
        self.selected_transport = resolve_transport_for_ff_tools(self.context.transport_mode)

    def emit(self, kind: str, **payload) -> None:
        self.event_queue.put({"type": kind, **payload})

    def request_stop(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass

    def run(self) -> None:
        self.started_at = datetime.now()
        self.monotonic_started = time.monotonic()
        try:
            self._probe_metadata()
            self._capture_snapshot_if_possible()
            if self.stop_event.is_set():
                self.ended_at = datetime.now()
                self.emit(
                    "completed",
                    report=self._build_report(return_code=255, error="Stopped by user before test start."),
                )
                return

            engine = normalize_engine_mode(self.context.engine_mode)
            if engine == "gstreamer":
                return_code, error = self._run_gstreamer_backend()
            else:
                return_code, error = self._run_ffmpeg_backend()
        except Exception as exc:
            self.ended_at = datetime.now()
            self.emit("completed", report=self._build_report(return_code=1, error=str(exc)))
            return

        self.ended_at = datetime.now()
        self._close_open_freeze_if_needed()
        self.emit("completed", report=self._build_report(return_code=return_code, error=error))

    def _probe_metadata(self) -> None:
        engine = normalize_engine_mode(self.context.engine_mode)
        if engine == "gstreamer":
            self.emit("status", message="Probing stream metadata via GStreamer...")
            self.stream_info = probe_stream_gstreamer(
                gst_launch_path=self.context.gst_launch_path,
                gst_play_path=self.context.gst_play_path,
                gst_discoverer_path=self.context.gst_discoverer_path,
                rtsp_url=self.context.rtsp_url,
                requested_transport=self.context.transport_mode,
            )
            self.selected_transport = resolve_transport_for_ff_tools(
                str(self.stream_info.get("selected_transport", self.selected_transport))
            )
            selected_test = (
                self.stream_info.get("transport_diagnostics", {}).get("tests", {}).get(self.selected_transport, {})
            )
            self.gstreamer_probe_result = {
                **selected_test,
                "discoverer": self.stream_info.get("discovery_backend", ""),
                "selected_transport": self.selected_transport,
            }
            self.emit(
                "log",
                line=(
                    "GStreamer metadata/transport probe passed: "
                    f"{Path(str(selected_test.get('binary', self.context.gst_launch_path or 'gst-launch-1.0'))).name} "
                    f"over {self.selected_transport.upper()}."
                ),
            )
            self.emit("stream_info", info=self.stream_info)
            return

        if self.context.ffprobe_path:
            self.emit("status", message="Probing stream metadata via ffprobe...")
            self.stream_info = probe_stream(
                self.context.ffprobe_path,
                self.context.rtsp_url,
                requested_transport=self.context.transport_mode,
            )
            self.selected_transport = resolve_transport_for_ff_tools(
                str(self.stream_info.get("selected_transport", self.selected_transport))
            )
            self.emit("stream_info", info=self.stream_info)
            return

        self.selected_transport = resolve_transport_for_ff_tools(self.context.transport_mode)
        self.stream_info = {
            "selected_transport": self.selected_transport,
            "requested_transport": normalize_transport_mode(self.context.transport_mode),
            "selected_delivery": transport_delivery_label(self.selected_transport),
            "requested_delivery": transport_delivery_label(self.context.transport_mode),
            "transport_diagnostics": {
                "requested": normalize_transport_mode(self.context.transport_mode),
                "selected": self.selected_transport,
                "tests": {},
            },
            "stream_count": 0,
            "video_stream_count": 0,
            "audio_stream_count": 0,
            "first_video_index": 0,
            "format_bit_rate_bps": 0,
            "format_bit_rate_kbps": 0.0,
            "streams": [],
            "codec_name": "unknown",
            "width": 0,
            "height": 0,
            "pix_fmt": "unknown",
            "avg_frame_rate_raw": "0/0",
            "r_frame_rate_raw": "0/0",
            "fps": 0.0,
            "bit_rate_kbps": 0.0,
        }
        self.emit(
            "status",
            message="FFprobe not found. Running in fallback mode using FFmpeg-only diagnostics.",
        )

    def _capture_snapshot_if_possible(self) -> None:
        if not self.context.ffmpeg_path:
            self.snapshot_error = "FFmpeg snapshot helper not available on this PC."
            self.emit("log", line=f"Snapshot capture skipped: {self.snapshot_error}")
            return

        snapshot_target = Path(self.context.output_dir) / f"snapshot_{self.context.run_id}.jpg"
        ok, snapshot_error = capture_rtsp_snapshot(
            ffmpeg_path=self.context.ffmpeg_path,
            rtsp_url=self.context.rtsp_url,
            output_path=snapshot_target,
            transport=self.selected_transport,
            timeout_sec=20,
        )
        if ok:
            self.snapshot_path = str(snapshot_target)
            self.emit("log", line=f"Snapshot captured: {snapshot_target}")
        else:
            self.snapshot_error = snapshot_error
            self.emit("log", line=f"Snapshot capture skipped: {snapshot_error}")

    def _run_ffmpeg_backend(self) -> tuple[int, str]:
        ffmpeg_cmd = [
            self.context.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "info",
            "-stats_period",
            "1",
            "-progress",
            "pipe:1",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-max_delay",
            "500000",
            "-rtsp_transport",
            self.selected_transport,
            "-t",
            str(self.context.duration_seconds),
            "-i",
            self.context.rtsp_url,
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v",
            "copy",
            "-f",
            "mpegts",
            "-y",
            os.devnull,
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-f",
            "null",
            "-",
        ]

        self.emit("status", message=f"Running FFmpeg diagnostics over {self.selected_transport.upper()}...")
        self.emit("command", command=" ".join(ffmpeg_cmd))

        try:
            self.process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            raise RuntimeError(f"Unable to launch ffmpeg: {exc}") from exc

        progress_thread = threading.Thread(target=self._read_progress_stream, daemon=True)
        log_thread = threading.Thread(target=self._read_log_stream, daemon=True)
        progress_thread.start()
        log_thread.start()

        while self.process.poll() is None:
            if self.stop_event.is_set():
                try:
                    self.process.terminate()
                except Exception:
                    pass
                break
            time.sleep(0.2)

        try:
            return_code = self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return_code = self.process.wait(timeout=5)

        progress_thread.join(timeout=2)
        log_thread.join(timeout=2)
        return return_code, ""

    def _build_gstreamer_command(self) -> list[str]:
        if not self.context.gst_launch_path:
            raise RuntimeError("GStreamer diagnostics require gst-launch-1.0.")
        gst_protocol = {
            "tcp": "tcp",
            "udp": "udp",
            "udp_multicast": "udp-mcast",
        }.get(self.selected_transport, "tcp")
        return [
            self.context.gst_launch_path,
            "-m",
            "uridecodebin",
            "name=src",
            f"uri={self.context.rtsp_url}",
            f"source::protocols={gst_protocol}",
            "source::latency=0",
            "src.",
            "!",
            "queue",
            "!",
            "videoconvert",
            "!",
            "progressreport",
            "update-freq=1",
            "silent=false",
            "!",
            "fakesink",
            "name=gs_video_sink",
            "sync=true",
            "src.",
            "!",
            "queue",
            "!",
            "audioconvert",
            "!",
            "fakesink",
            "name=gs_audio_sink",
            "sync=false",
        ]

    def _run_gstreamer_backend(self) -> tuple[int, str]:
        gst_cmd = self._build_gstreamer_command()
        gst_env = build_gstreamer_env(self.context.gst_launch_path or self.context.gst_play_path)
        self.gstreamer_runtime_details = {
            "launch_binary": self.context.gst_launch_path,
            "discoverer_binary": self.context.gst_discoverer_path,
            "transport": self.selected_transport,
            "progress_phases": [],
            "decoder_element": "",
            "device_context": "",
            "bitrate_source": "gstreamer_tag_messages",
            "packet_stats": {},
            "rtp_elements": [],
            "decode_elements": [],
        }

        self.emit("status", message=f"Running GStreamer diagnostics over {self.selected_transport.upper()}...")
        self.emit("command", command=" ".join(gst_cmd))

        try:
            self.process = subprocess.Popen(
                gst_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=gst_env,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            raise RuntimeError(f"Unable to launch GStreamer diagnostics: {exc}") from exc

        stdout_thread = threading.Thread(target=self._read_gstreamer_stdout, daemon=True)
        stderr_thread = threading.Thread(target=self._read_gstreamer_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        error = ""
        while self.process.poll() is None:
            progress_elapsed = float(self.last_progress.get("analysis_elapsed_sec", 0.0) or 0.0)
            if progress_elapsed >= float(self.context.duration_seconds):
                self.duration_complete = True
                try:
                    self.process.terminate()
                except Exception:
                    pass
                break
            if self.stop_event.is_set():
                try:
                    self.process.terminate()
                except Exception:
                    pass
                break
            time.sleep(0.2)

        try:
            return_code = self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return_code = self.process.wait(timeout=5)

        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if self.duration_complete and return_code != 0 and not self.stop_event.is_set():
            return_code = 0
        if self.duration_complete and not self.last_progress:
            error = "GStreamer did not emit timed progress samples before the run ended."
        return return_code, error

    def _register_warning_line(self, line: str) -> None:
        if not should_count_warning(line):
            return
        self.warning_count += 1
        category = classify_warning(line)
        self.warning_breakdown[category] = self.warning_breakdown.get(category, 0) + 1
        self.rtp_missed_packets += extract_missed_packets(line)
        if len(self.warning_samples) < MAX_WARNING_SAMPLES:
            self.warning_samples.append(line)

    def _append_gstreamer_phase(self, phase: str) -> None:
        phase = str(phase or "").strip()
        if not phase:
            return
        phases = self.gstreamer_runtime_details.setdefault("progress_phases", [])
        if phase not in phases:
            phases.append(phase)

    def _inspect_gstreamer_runtime_line(self, line: str) -> None:
        lowered = line.lower()
        element_match = re.search(r'from element "([^"]+)"', line)
        if element_match:
            element_name = element_match.group(1)
            element_lower = element_name.lower()
            if any(token in element_lower for token in ("rtp", "jitterbuffer", "depay", "session", "storage", "ssrcdemux")):
                rtp_elements = self.gstreamer_runtime_details.setdefault("rtp_elements", [])
                if element_name not in rtp_elements and len(rtp_elements) < 16:
                    rtp_elements.append(element_name)
            if any(token in element_lower for token in ("parse", "dec", "decodebin", "convert", "capsfilter")):
                decode_elements = self.gstreamer_runtime_details.setdefault("decode_elements", [])
                if element_name not in decode_elements and len(decode_elements) < 16:
                    decode_elements.append(element_name)
            existing_decoder = str(self.gstreamer_runtime_details.get("decoder_element", "") or "")
            existing_lower = existing_decoder.lower()
            decoder_tokens = (
                "decodebin",
                "decoder",
                "avdec",
                "d3d11",
                "d3d12",
                "nvh",
                "nvdec",
                "vaapi",
                "msdk",
                "qsv",
                "openh264",
                "jpegdec",
                "vp8dec",
                "vp9dec",
            )
            video_decoder_tokens = ("h264", "h265", "hevc", "vp8", "vp9", "jpeg", "d3d11", "d3d12", "nv", "vaapi", "qsv", "msdk")
            audio_decoder_tokens = ("aac", "mp3", "opus", "vorbis", "flac", "audio")
            if (
                any(token in element_lower for token in decoder_tokens)
                and (
                    not existing_decoder
                    or existing_lower.startswith("decodebin")
                    or (
                        any(token in element_lower for token in video_decoder_tokens)
                        and any(token in existing_lower for token in audio_decoder_tokens)
                    )
                )
            ):
                self.gstreamer_runtime_details["decoder_element"] = element_name

        for keyword, label in (
            ("d3d11", "D3D11"),
            ("d3d12", "D3D12"),
            ("direct3d11", "D3D11"),
            ("cuda", "CUDA"),
            ("nvcodec", "NVIDIA NVCodec"),
            ("nvdec", "NVIDIA NVDEC"),
            ("vaapi", "VAAPI"),
            ("qsv", "Intel QSV"),
            ("msdk", "Intel Media SDK"),
            ("dxva", "DXVA"),
            ("opengl", "OpenGL"),
        ):
            if keyword in lowered:
                self.gstreamer_runtime_details["device_context"] = label
                break

        if "setting pipeline to paused" in lowered:
            self._append_gstreamer_phase("PAUSED")
        elif "setting pipeline to playing" in lowered:
            self._append_gstreamer_phase("PLAYING")
        elif "pipeline is live" in lowered:
            self._append_gstreamer_phase("LIVE_SOURCE")
        elif "pipeline is prerolled" in lowered or "prerolled" in lowered:
            self._append_gstreamer_phase("PREROLLED")
        elif "new clock" in lowered:
            self._append_gstreamer_phase("CLOCK_LOCKED")
        elif "redistribute latency" in lowered:
            self._append_gstreamer_phase("LATENCY_REDISTRIBUTION")
        elif "eos" in lowered:
            self._append_gstreamer_phase("EOS")

        bitrate_bps = parse_gst_bitrate_bps(line)
        if bitrate_bps and bitrate_bps > 0:
            bitrate_kbps = round(bitrate_bps / 1000.0, 3)
            if "audio" in lowered and "video" not in lowered:
                self.latest_gstreamer_audio_bitrate_kbps = bitrate_kbps
            else:
                self.latest_gstreamer_bitrate_kbps = bitrate_kbps

        packet_stats = parse_gst_packet_stats(line)
        if any(value > 0 for value in packet_stats.values()):
            runtime_packet_stats = self.gstreamer_runtime_details.setdefault("packet_stats", {})
            for key, value in packet_stats.items():
                if value > 0:
                    runtime_packet_stats[key] = max(int(runtime_packet_stats.get(key, 0) or 0), int(value))
            self.rtp_missed_packets = max(
                self.rtp_missed_packets,
                int(runtime_packet_stats.get("packets_lost", self.rtp_missed_packets) or self.rtp_missed_packets),
            )

    def _read_gstreamer_stdout(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for raw_line in self.process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            self.emit("log", line=line)
            self._inspect_gstreamer_runtime_line(line)
            self._register_warning_line(line)
            media_elapsed = parse_gst_progress_seconds(line)
            if media_elapsed is None:
                continue
            snapshot = self._parse_gstreamer_progress_snapshot(media_elapsed=media_elapsed, line=line)
            self.last_progress = snapshot
            self.emit("progress", data=snapshot)

    def _read_gstreamer_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        for raw_line in self.process.stderr:
            line = raw_line.rstrip()
            if not line:
                continue
            self.emit("log", line=line)
            self._inspect_gstreamer_runtime_line(line)
            self._register_warning_line(line)
            media_elapsed = parse_gst_progress_seconds(line)
            if media_elapsed is None:
                continue
            snapshot = self._parse_gstreamer_progress_snapshot(media_elapsed=media_elapsed, line=line)
            self.last_progress = snapshot
            self.emit("progress", data=snapshot)

    def _parse_gstreamer_progress_snapshot(self, *, media_elapsed: float, line: str) -> dict:
        analysis_elapsed = max(0.0, float(media_elapsed))
        if self.context.duration_seconds > 0:
            analysis_elapsed = min(analysis_elapsed, float(self.context.duration_seconds))
        if self.last_elapsed_sec is not None:
            analysis_elapsed = max(analysis_elapsed, self.last_elapsed_sec)

        current_wall_monotonic = time.monotonic()
        if self.first_media_progress_wall is None and analysis_elapsed > 0:
            self.first_media_progress_wall = current_wall_monotonic
            self.first_frame_media_ts = analysis_elapsed

        wall_elapsed = 0.0
        if self.first_media_progress_wall is not None:
            wall_elapsed = max(
                0.0,
                current_wall_monotonic - self.first_media_progress_wall + float(self.first_frame_media_ts or 0.0),
            )
            if self.context.duration_seconds > 0:
                wall_elapsed = min(wall_elapsed, float(self.context.duration_seconds))

        fps_nominal = float(self.stream_info.get("fps", 0.0) or 0.0)
        if fps_nominal <= 0:
            fps_nominal = self.fallback_nominal_fps or 25.0
            self.fallback_nominal_fps = fps_nominal

        frame = int(round(max(0.0, analysis_elapsed) * fps_nominal)) if fps_nominal > 0 else int(self.last_frame_count or 0)

        instant_fps = 0.0
        if self.last_elapsed_sec is not None and self.last_wall_sample_sec is not None:
            media_delta = max(0.0, analysis_elapsed - self.last_elapsed_sec)
            wall_delta = max(0.0, current_wall_monotonic - self.last_wall_sample_sec)
            if wall_delta > 0:
                instant_fps = (frame - int(self.last_frame_count or 0)) / wall_delta if frame >= int(self.last_frame_count or 0) else 0.0
                if instant_fps >= 0:
                    self.frame_rate_samples.append(instant_fps)
                    self.realtime_fps_samples.append(instant_fps)

                lag_sec = max(0.0, wall_delta - media_delta)
                self.gstreamer_wall_drift_samples.append(lag_sec)
                if lag_sec > 0.12 and fps_nominal > 0:
                    self.gstreamer_drop_equivalent_frames += fps_nominal * lag_sec
                if lag_sec >= 1.0:
                    self.freeze_events.append(
                        {
                            "start_media_sec": round(self.last_elapsed_sec, 3),
                            "duration_sec": round(lag_sec, 3),
                        }
                    )

        current_bandwidth_kbps = 0.0
        if self.latest_gstreamer_bitrate_kbps > 0:
            current_bandwidth_kbps += self.latest_gstreamer_bitrate_kbps
        elif float(self.stream_info.get("bit_rate_kbps", 0.0) or 0.0) > 0:
            current_bandwidth_kbps += float(self.stream_info.get("bit_rate_kbps", 0.0) or 0.0)
        elif float(self.stream_info.get("format_bit_rate_kbps", 0.0) or 0.0) > 0:
            current_bandwidth_kbps += float(self.stream_info.get("format_bit_rate_kbps", 0.0) or 0.0)

        if self.latest_gstreamer_audio_bitrate_kbps > 0:
            current_bandwidth_kbps += self.latest_gstreamer_audio_bitrate_kbps

        if current_bandwidth_kbps > 0:
            self.bitrate_kbps_samples.append(current_bandwidth_kbps)

        expected_frames = frame + max(0.0, self.gstreamer_drop_equivalent_frames)
        if not self.drop_baseline_set:
            if frame > 0 and expected_frames > 0:
                self.drop_baseline_bias = max(0.0, expected_frames - float(frame))
                self.drop_baseline_set = True
        expected_frames = max(float(frame), expected_frames - self.drop_baseline_bias)
        estimated_drop = max(0, int(round(expected_frames - frame))) if expected_frames > 0 else 0
        drop_rate_percent = (estimated_drop / expected_frames * 100.0) if expected_frames > 0 else 0.0
        jitter_std = safe_stdev(self.frame_rate_samples)
        jitter_percent = (jitter_std / fps_nominal * 100.0) if fps_nominal > 0 else 0.0
        startup_latency = (
            max(0.0, self.first_media_progress_wall - self.monotonic_started)
            if self.first_media_progress_wall
            else 0.0
        )
        freeze_total = sum(float(item.get("duration_sec", 0.0) or 0.0) for item in self.freeze_events)
        freeze_ratio_percent = (freeze_total / analysis_elapsed * 100.0) if analysis_elapsed > 0 else 0.0
        health_score, health_grade = compute_health_score(
            drop_rate_percent=drop_rate_percent,
            fps_jitter_percent=jitter_percent,
            warning_count=self.warning_count,
            freeze_ratio_percent=freeze_ratio_percent,
            startup_latency_sec=startup_latency,
            missed_packets=self.rtp_missed_packets,
        )

        overall_speed = 0.0
        if wall_elapsed > 0:
            overall_speed = analysis_elapsed / wall_elapsed
            self.speed_samples.append(overall_speed)

        timeline_point = {
            "elapsed_sec": round(analysis_elapsed, 3),
            "analysis_elapsed_sec": round(analysis_elapsed, 3),
            "gstreamer_media_elapsed_sec": round(media_elapsed, 3),
            "wall_elapsed_sec": round(wall_elapsed, 3),
            "frame": int(frame),
            "expected_frames": round(expected_frames, 3),
            "estimated_drop_frames": int(estimated_drop),
            "drop_rate_percent": round(drop_rate_percent, 3),
            "bandwidth_kbps_current": round(current_bandwidth_kbps, 3),
            "fps_realtime_num": round(instant_fps, 3),
            "health_score": int(health_score),
            "warning_count": int(self.warning_count),
        }
        if self.timeline_samples:
            previous_point = self.timeline_samples[-1]
            previous_elapsed = float(
                previous_point.get("analysis_elapsed_sec", previous_point.get("elapsed_sec", 0.0)) or 0.0
            )
            if abs(previous_elapsed - float(timeline_point["analysis_elapsed_sec"])) < 0.001:
                self.timeline_samples[-1] = timeline_point
            else:
                self.timeline_samples.append(timeline_point)
        else:
            self.timeline_samples.append(timeline_point)
        if len(self.timeline_samples) > 12000:
            self.timeline_samples = self.timeline_samples[-12000:]

        self.last_elapsed_sec = analysis_elapsed
        self.last_frame_count = frame
        self.last_wall_sample_sec = current_wall_monotonic

        return {
            "elapsed_sec": round(analysis_elapsed, 3),
            "analysis_elapsed_sec": round(analysis_elapsed, 3),
            "gstreamer_media_elapsed_sec": round(media_elapsed, 3),
            "wall_elapsed_sec": round(wall_elapsed, 3),
            "frame": frame,
            "dup_frames": 0,
            "ffmpeg_drop_frames": 0,
            "expected_frames": round(expected_frames, 3),
            "estimated_drop_frames": estimated_drop,
            "drop_rate_percent": round(drop_rate_percent, 3),
            "progress_state": "end" if analysis_elapsed >= float(self.context.duration_seconds) else "continue",
            "speed": f"{overall_speed:.3f}x",
            "fps_realtime": f"{instant_fps:.3f}",
            "fps_realtime_num": round(instant_fps, 3),
            "bitrate": f"{current_bandwidth_kbps:.1f} kbps" if current_bandwidth_kbps > 0 else "N/A",
            "bandwidth_kbps_current": round(current_bandwidth_kbps, 3),
            "bandwidth_kbps_avg": round(safe_mean(self.bitrate_kbps_samples), 3),
            "total_size_bytes": 0,
            "instant_fps_avg": round(safe_mean(self.frame_rate_samples), 3),
            "instant_fps_jitter_std": round(jitter_std, 3),
            "instant_fps_jitter_percent": round(jitter_percent, 3),
            "startup_latency_sec": round(startup_latency, 3),
            "rtp_missed_packets": int(self.rtp_missed_packets),
            "health_score": health_score,
            "health_grade": health_grade,
            "drop_baseline_bias_frames": 0.0,
        }

    def _read_progress_stream(self) -> None:
        if not self.process or not self.process.stdout:
            return
        block: dict[str, str] = {}
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            block[key] = value
            if key == "progress":
                snapshot = self._parse_progress_snapshot(block)
                self.last_progress = snapshot
                self.emit("progress", data=snapshot)
                block = {}

    def _read_log_stream(self) -> None:
        if not self.process or not self.process.stderr:
            return
        for raw_line in self.process.stderr:
            line = raw_line.rstrip()
            if not line:
                continue
            self.emit("log", line=line)
            self._register_warning_line(line)

    def _parse_progress_snapshot(self, payload: dict[str, str]) -> dict:
        frame = int(payload.get("frame", "0") or "0")
        dup_frames = int(payload.get("dup_frames", "0") or "0")
        dropped_by_ffmpeg = int(payload.get("drop_frames", "0") or "0")
        total_size_bytes = parse_int_token(payload.get("total_size", "0"))

        if payload.get("out_time_us"):
            try:
                elapsed = float(payload["out_time_us"]) / 1_000_000.0
            except ValueError:
                elapsed = 0.0
        elif payload.get("out_time_ms"):
            try:
                elapsed = float(payload["out_time_ms"]) / 1_000_000.0
            except ValueError:
                elapsed = 0.0
        else:
            elapsed = hhmmss_to_seconds(payload.get("out_time", "00:00:00"))

        progress_state = payload.get("progress", "continue")
        analysis_elapsed = max(0.0, elapsed)
        if self.context.duration_seconds > 0:
            analysis_elapsed = min(analysis_elapsed, float(self.context.duration_seconds))
        if self.last_elapsed_sec is not None:
            if (
                self.last_frame_count is not None
                and frame <= self.last_frame_count
                and analysis_elapsed <= self.last_elapsed_sec
            ):
                # FFmpeg can emit a closing snapshot that reports an older or repeated media timestamp.
                # Keep the analytics timeline monotonic so the report does not invent end-of-run drops.
                analysis_elapsed = self.last_elapsed_sec
            elif (
                progress_state == "end"
                and self.last_frame_count is not None
                and frame <= self.last_frame_count
                and analysis_elapsed >= self.last_elapsed_sec
            ):
                # FFmpeg can also emit a final "end" snapshot with more out_time but no new frames.
                # Reuse the last analytics timestamp instead of extending the test after frames stopped.
                analysis_elapsed = self.last_elapsed_sec

        if self.first_media_progress_wall is None and (frame > 0 or elapsed > 0):
            self.first_media_progress_wall = time.monotonic()
            self.first_frame_media_ts = analysis_elapsed

        bitrate_kbps = parse_bitrate_to_kbps(payload.get("bitrate", ""))
        current_bandwidth_kbps = 0.0

        realtime_fps = parse_float_token(payload.get("fps", ""))
        if realtime_fps is not None and realtime_fps >= 0:
            self.realtime_fps_samples.append(realtime_fps)

        speed_value = parse_float_token(payload.get("speed", ""))
        if speed_value is not None and speed_value >= 0:
            self.speed_samples.append(speed_value)

        if self.last_elapsed_sec is not None and self.last_frame_count is not None:
            elapsed_delta = analysis_elapsed - self.last_elapsed_sec
            frame_delta = frame - self.last_frame_count
            if elapsed_delta > 0:
                instant_fps = frame_delta / elapsed_delta
                if elapsed_delta >= 0.25 and instant_fps >= 0:
                    self.frame_rate_samples.append(instant_fps)

                if total_size_bytes > self.last_total_size_bytes:
                    size_delta = total_size_bytes - self.last_total_size_bytes
                    current_bandwidth_kbps = (size_delta * 8.0) / max(elapsed_delta, 0.001) / 1000.0

                if elapsed_delta >= 1.0 and frame_delta <= 0 and frame > 0:
                    if self.freeze_start_media_ts is None:
                        self.freeze_start_media_ts = self.last_elapsed_sec
                elif frame_delta > 0 and self.freeze_start_media_ts is not None:
                    freeze_duration = max(0.0, analysis_elapsed - self.freeze_start_media_ts)
                    if freeze_duration >= 1.0:
                        self.freeze_events.append(
                            {
                                "start_media_sec": round(self.freeze_start_media_ts, 3),
                                "duration_sec": round(freeze_duration, 3),
                            }
                        )
                    self.freeze_start_media_ts = None

        if bitrate_kbps is None or bitrate_kbps <= 0:
            if current_bandwidth_kbps > 0:
                bitrate_kbps = current_bandwidth_kbps
            elif total_size_bytes > 0 and elapsed > 0:
                bitrate_kbps = (total_size_bytes * 8.0) / elapsed / 1000.0

        if bitrate_kbps is not None and bitrate_kbps > 0:
            self.bitrate_kbps_samples.append(bitrate_kbps)

        self.last_elapsed_sec = analysis_elapsed
        self.last_frame_count = frame
        self.last_total_size_bytes = total_size_bytes

        fps_nominal = float(self.stream_info.get("fps", 0.0) or 0.0)
        if fps_nominal <= 0 and self.frame_rate_samples:
            fps_nominal = safe_mean(self.frame_rate_samples[-15:])
            self.fallback_nominal_fps = fps_nominal

        raw_expected_frames = fps_nominal * analysis_elapsed if fps_nominal > 0 else 0.0
        if not self.drop_baseline_set:
            if frame > 0 and raw_expected_frames > 0:
                self.drop_baseline_bias = max(0.0, raw_expected_frames - float(frame))
                self.drop_baseline_set = True
            else:
                raw_expected_frames = 0.0

        expected_frames = max(0.0, raw_expected_frames - self.drop_baseline_bias)
        estimated_drop = max(0, int(round(expected_frames - frame))) if expected_frames > 0 else 0
        drop_rate_percent = (estimated_drop / expected_frames * 100.0) if expected_frames > 0 else 0.0
        jitter_std = safe_stdev(self.frame_rate_samples)
        jitter_percent = (jitter_std / fps_nominal * 100.0) if fps_nominal > 0 else 0.0
        startup_latency = (
            max(0.0, self.first_media_progress_wall - self.monotonic_started)
            if self.first_media_progress_wall
            else 0.0
        )
        freeze_total = self._freeze_total_seconds(current_elapsed=analysis_elapsed)
        freeze_ratio_percent = (freeze_total / analysis_elapsed * 100.0) if analysis_elapsed > 0 else 0.0
        health_score, health_grade = compute_health_score(
            drop_rate_percent=drop_rate_percent,
            fps_jitter_percent=jitter_percent,
            warning_count=self.warning_count,
            freeze_ratio_percent=freeze_ratio_percent,
            startup_latency_sec=startup_latency,
            missed_packets=self.rtp_missed_packets,
        )

        timeline_point = {
            "elapsed_sec": round(analysis_elapsed, 3),
            "ffmpeg_elapsed_sec": round(elapsed, 3),
            "analysis_elapsed_sec": round(analysis_elapsed, 3),
            "frame": int(frame),
            "expected_frames": round(expected_frames, 3),
            "estimated_drop_frames": int(estimated_drop),
            "drop_rate_percent": round(drop_rate_percent, 3),
            "bandwidth_kbps_current": round(bitrate_kbps, 3) if bitrate_kbps is not None else 0.0,
            "total_size_bytes": int(total_size_bytes),
            "fps_realtime_num": round(realtime_fps, 3) if realtime_fps is not None else 0.0,
            "health_score": int(health_score),
            "warning_count": int(self.warning_count),
        }
        if self.timeline_samples:
            previous_point = self.timeline_samples[-1]
            previous_elapsed = float(previous_point.get("analysis_elapsed_sec", previous_point.get("elapsed_sec", 0.0)) or 0.0)
            if abs(previous_elapsed - float(timeline_point["analysis_elapsed_sec"])) < 0.001:
                self.timeline_samples[-1] = timeline_point
            else:
                self.timeline_samples.append(timeline_point)
        else:
            self.timeline_samples.append(timeline_point)
        if len(self.timeline_samples) > 12000:
            self.timeline_samples = self.timeline_samples[-12000:]

        return {
            "elapsed_sec": round(analysis_elapsed, 3),
            "ffmpeg_elapsed_sec": round(elapsed, 3),
            "analysis_elapsed_sec": round(analysis_elapsed, 3),
            "frame": frame,
            "dup_frames": dup_frames,
            "ffmpeg_drop_frames": dropped_by_ffmpeg,
            "expected_frames": round(expected_frames, 3),
            "estimated_drop_frames": estimated_drop,
            "drop_rate_percent": round(drop_rate_percent, 3),
            "progress_state": payload.get("progress", "continue"),
            "speed": payload.get("speed", "N/A"),
            "fps_realtime": payload.get("fps", "N/A"),
            "fps_realtime_num": round(realtime_fps, 3) if realtime_fps is not None else 0.0,
            "bitrate": payload.get("bitrate", "N/A"),
            "bandwidth_kbps_current": round(bitrate_kbps, 3) if bitrate_kbps is not None else 0.0,
            "bandwidth_kbps_avg": round(safe_mean(self.bitrate_kbps_samples), 3),
            "total_size_bytes": int(total_size_bytes),
            "instant_fps_avg": round(safe_mean(self.frame_rate_samples), 3),
            "instant_fps_jitter_std": round(jitter_std, 3),
            "instant_fps_jitter_percent": round(jitter_percent, 3),
            "startup_latency_sec": round(startup_latency, 3),
            "rtp_missed_packets": int(self.rtp_missed_packets),
            "health_score": health_score,
            "health_grade": health_grade,
            "drop_baseline_bias_frames": round(self.drop_baseline_bias, 3),
        }

    def _freeze_total_seconds(self, current_elapsed: float = 0.0) -> float:
        finished = sum(float(item.get("duration_sec", 0.0) or 0.0) for item in self.freeze_events)
        if self.freeze_start_media_ts is None:
            return finished
        in_progress = max(0.0, current_elapsed - self.freeze_start_media_ts)
        return finished + in_progress

    def _close_open_freeze_if_needed(self) -> None:
        if self.freeze_start_media_ts is None:
            return
        tail_elapsed = self.last_elapsed_sec or self.first_frame_media_ts or 0.0
        freeze_duration = max(0.0, tail_elapsed - self.freeze_start_media_ts)
        if freeze_duration >= 1.0:
            self.freeze_events.append(
                {
                    "start_media_sec": round(self.freeze_start_media_ts, 3),
                    "duration_sec": round(freeze_duration, 3),
                }
            )
        self.freeze_start_media_ts = None

    def _build_report(self, return_code: int, error: str = "") -> dict:
        ended = self.ended_at or datetime.now()
        started = self.started_at or ended
        elapsed_wall = max(0.0, (ended - started).total_seconds())
        engine_mode = normalize_engine_mode(self.context.engine_mode)
        analytics_backend = "gstreamer" if engine_mode == "gstreamer" else "ffmpeg"
        validation_backend = "gstreamer" if engine_mode == "gstreamer" else ("ffprobe" if self.context.ffprobe_path else "ffmpeg")

        final_progress = self.last_progress or {}
        stream_fps = float(self.stream_info.get("fps", 0.0) or 0.0)
        if stream_fps <= 0:
            stream_fps = self.fallback_nominal_fps or safe_mean(self.frame_rate_samples)
        media_elapsed = float(final_progress.get("elapsed_sec", 0.0) or 0.0)
        frames_received = int(final_progress.get("frame", 0) or 0)
        expected_frames = float(final_progress.get("expected_frames", 0.0) or 0.0)
        estimated_drops = int(final_progress.get("estimated_drop_frames", 0) or 0)
        ffmpeg_drops = int(final_progress.get("ffmpeg_drop_frames", 0) or 0)
        ffmpeg_dups = int(final_progress.get("dup_frames", 0) or 0)

        status = "completed"
        if self.stop_event.is_set():
            status = "stopped"
        elif return_code != 0:
            status = "failed"

        achieved_fps = (frames_received / media_elapsed) if media_elapsed > 0 else 0.0
        estimated_drop_rate_percent = (estimated_drops / expected_frames * 100.0) if expected_frames > 0 else 0.0
        ffmpeg_drop_rate_percent = (ffmpeg_drops / max(frames_received + ffmpeg_drops, 1) * 100.0)

        bitrate_avg = safe_mean(self.bitrate_kbps_samples)
        bitrate_min = min(self.bitrate_kbps_samples) if self.bitrate_kbps_samples else 0.0
        bitrate_max = max(self.bitrate_kbps_samples) if self.bitrate_kbps_samples else 0.0
        bitrate_p95 = safe_percentile(self.bitrate_kbps_samples, 95.0)
        estimated_data_mb = (bitrate_avg * media_elapsed / 8.0 / 1024.0) if bitrate_avg > 0 and media_elapsed > 0 else 0.0

        realtime_fps_avg = safe_mean(self.realtime_fps_samples)
        realtime_fps_min = min(self.realtime_fps_samples) if self.realtime_fps_samples else 0.0
        realtime_fps_max = max(self.realtime_fps_samples) if self.realtime_fps_samples else 0.0
        speed_avg = safe_mean(self.speed_samples)
        speed_min = min(self.speed_samples) if self.speed_samples else 0.0
        speed_max = max(self.speed_samples) if self.speed_samples else 0.0
        frame_fps_avg = safe_mean(self.frame_rate_samples)
        frame_fps_jitter_std = safe_stdev(self.frame_rate_samples)
        frame_fps_jitter_percent = (
            frame_fps_jitter_std / stream_fps * 100.0 if stream_fps > 0 else 0.0
        )

        freeze_total_sec = sum(float(item.get("duration_sec", 0.0) or 0.0) for item in self.freeze_events)
        freeze_ratio_percent = (freeze_total_sec / media_elapsed * 100.0) if media_elapsed > 0 else 0.0
        startup_latency_sec = (
            max(0.0, self.first_media_progress_wall - self.monotonic_started)
            if self.first_media_progress_wall
            else 0.0
        )

        health_score, health_grade = compute_health_score(
            drop_rate_percent=estimated_drop_rate_percent,
            fps_jitter_percent=frame_fps_jitter_percent,
            warning_count=self.warning_count,
            freeze_ratio_percent=freeze_ratio_percent,
            startup_latency_sec=startup_latency_sec,
            missed_packets=self.rtp_missed_packets,
        )

        deep_diagnostics = {
            "ffprobe_used": bool(self.context.ffprobe_path and engine_mode != "gstreamer"),
            "transport": self.stream_info.get("transport_diagnostics", {}),
            "gstreamer_probe": self.gstreamer_probe_result,
            "gstreamer_runtime": self.gstreamer_runtime_details,
            "startup_latency_sec": round(startup_latency_sec, 3),
            "bandwidth_kbps": {
                "avg": round(bitrate_avg, 3),
                "min": round(bitrate_min, 3),
                "max": round(bitrate_max, 3),
                "p95": round(bitrate_p95, 3),
                "source": "gstreamer_tag_messages" if engine_mode == "gstreamer" else "ffmpeg_progress_total_size",
            },
            "estimated_total_data_mb": round(estimated_data_mb, 3),
            "fps_analysis": {
                "stream_nominal_fps": round(stream_fps, 3),
                "decoded_fps_avg": round(frame_fps_avg, 3),
                "realtime_fps_avg": round(realtime_fps_avg, 3),
                "realtime_fps_min": round(realtime_fps_min, 3),
                "realtime_fps_max": round(realtime_fps_max, 3),
                "achieved_fps_by_frames_over_time": round(achieved_fps, 3),
                "jitter_std_fps": round(frame_fps_jitter_std, 3),
                "jitter_percent_of_nominal": round(frame_fps_jitter_percent, 3),
            },
            "speed_analysis": {
                "avg": round(speed_avg, 3),
                "min": round(speed_min, 3),
                "max": round(speed_max, 3),
            },
            "drop_analysis": {
                "diagnostic_model": (
                    "gstreamer_media_progress_vs_wall_clock"
                    if engine_mode == "gstreamer"
                    else "ffmpeg_progress_vs_nominal_fps"
                ),
                "estimated_drop_rate_percent": round(estimated_drop_rate_percent, 3),
                "ffmpeg_drop_rate_percent": round(ffmpeg_drop_rate_percent, 3),
                "gstreamer_wall_clock_drift_sec_avg": round(safe_mean(self.gstreamer_wall_drift_samples), 3),
                "gstreamer_wall_clock_drift_sec_max": round(max(self.gstreamer_wall_drift_samples), 3)
                if self.gstreamer_wall_drift_samples
                else 0.0,
                "edge_normalization": {
                    "startup_bias_frames_removed": round(self.drop_baseline_bias, 3),
                    "analysis_duration_capped_to_requested_duration": True,
                    "tail_end_snapshot_ignored_when_no_new_frames": engine_mode != "gstreamer",
                },
            },
            "freeze_analysis": {
                "freeze_events_count": len(self.freeze_events),
                "freeze_total_sec": round(freeze_total_sec, 3),
                "freeze_ratio_percent": round(freeze_ratio_percent, 3),
                "events": self.freeze_events,
            },
            "warnings_breakdown": self.warning_breakdown,
            "packet_indicators": {
                "rtp_missed_packets": int(self.rtp_missed_packets),
                "gstreamer_packet_stats": self.gstreamer_runtime_details.get("packet_stats", {}),
            },
            "health": {
                "score": int(health_score),
                "grade": health_grade,
            },
        }

        return {
            "app": APP_TITLE,
            "version": APP_VERSION,
            "build_date": BUILD_DATE,
            "run_id": self.context.run_id,
            "engine": {
                "requested": engine_mode,
                "validation_backend": validation_backend,
                "analytics_backend": analytics_backend,
            },
            "status": status,
            "return_code": return_code,
            "error": error,
            "started_at": started.isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds"),
            "requested_duration_sec": self.context.duration_seconds,
            "wall_clock_duration_sec": round(elapsed_wall, 3),
            "rtsp_url": self.context.rtsp_url,
            "transport": {
                "requested": normalize_transport_mode(self.context.transport_mode),
                "selected": self.selected_transport,
                "requested_delivery": transport_delivery_label(self.context.transport_mode),
                "selected_delivery": transport_delivery_label(self.selected_transport),
                "tests": self.stream_info.get("transport_diagnostics", {}).get("tests", {}),
            },
            "tool_paths": {
                "ffmpeg": self.context.ffmpeg_path,
                "ffprobe": self.context.ffprobe_path,
                "gst_launch": self.context.gst_launch_path,
                "gst_play": self.context.gst_play_path,
                "gst_discoverer": self.context.gst_discoverer_path,
            },
            "snapshot": {
                "path": self.snapshot_path,
                "error": self.snapshot_error,
            },
            "stream_info": self.stream_info,
            "summary": {
                "stream_nominal_fps": stream_fps,
                "frames_received": frames_received,
                "expected_frames": expected_frames,
                "estimated_dropped_frames": estimated_drops,
                "ffmpeg_reported_dropped_frames": ffmpeg_drops,
                "ffmpeg_reported_duplicated_frames": ffmpeg_dups,
                "media_elapsed_sec": media_elapsed,
                "realtime_fps_last": str(final_progress.get("fps_realtime", "N/A")),
                "speed_last": str(final_progress.get("speed", "N/A")),
                "health_score": int(health_score),
                "health_grade": health_grade,
                "transport_selected": self.selected_transport,
                "transport_delivery_selected": transport_delivery_label(self.selected_transport),
                "stream_count": int(self.stream_info.get("stream_count", 0) or 0),
                "video_stream_count": int(self.stream_info.get("video_stream_count", 0) or 0),
                "audio_stream_count": int(self.stream_info.get("audio_stream_count", 0) or 0),
            },
            "deep_diagnostics": deep_diagnostics,
            "timeline": self.timeline_samples,
            "warning_count": self.warning_count,
            "warning_samples": self.warning_samples,
        }


def _build_diagnosis_narrative(
    *,
    health_score: int,
    health_grade: str,
    estimated_drops: int,
    frames_received: int,
    warning_count: int,
    drop_rate: float,
    startup_latency: float,
    freeze_total: float,
    missed_packets: int,
    fps_jitter: float,
    bw_avg: float,
    bw_p95: float,
    status: str,
) -> tuple[str, list[str]]:
    """Return (executive_summary_text, list_of_recommendations)."""
    total = max(1, frames_received + max(0, estimated_drops))
    drop_pct = drop_rate

    if health_grade == "Excellent":
        headline = (
            f"The camera stream is performing excellently (score {health_score}/100). "
            "All key indicators are within healthy ranges."
        )
    elif health_grade == "Good":
        headline = (
            f"The camera stream is performing well (score {health_score}/100). "
            "Minor issues were detected but the stream is generally stable."
        )
    elif health_grade == "Fair":
        headline = (
            f"The camera stream shows moderate issues (score {health_score}/100). "
            "Some frames are being lost and quality may be impacted."
        )
    else:
        headline = (
            f"The camera stream is in poor condition (score {health_score}/100). "
            "Significant problems were detected that require attention."
        )

    if status == "failed":
        headline = "The diagnostic run failed to connect or complete. " + headline
    elif status == "stopped":
        headline += " Note: the run was stopped manually before completion."

    detail_parts: list[str] = []
    if estimated_drops > 0:
        detail_parts.append(
            f"{estimated_drops} frames were dropped ({drop_pct:.1f}% drop rate out of "
            f"{frames_received + estimated_drops} expected)."
        )
    else:
        detail_parts.append("No frame drops detected during this diagnostic run.")

    if freeze_total > 0:
        detail_parts.append(f"Stream froze for a total of {freeze_total:.1f}s during the run.")

    if missed_packets > 0:
        detail_parts.append(
            f"{missed_packets} missed RTP packets were detected, suggesting network packet loss."
        )

    if startup_latency > 3.0:
        detail_parts.append(
            f"Stream took {startup_latency:.1f}s to start, which is above the recommended 3s threshold."
        )

    if fps_jitter > 15.0:
        detail_parts.append(
            f"FPS jitter is {fps_jitter:.1f}% of nominal, indicating unstable frame timing."
        )

    if warning_count > 0:
        detail_parts.append(f"{warning_count} diagnostic warnings were logged during the run.")

    if bw_avg > 0:
        detail_parts.append(
            f"Average bandwidth was {bw_avg:.0f} kbps (P95: {bw_p95:.0f} kbps)."
        )

    summary_text = headline + " " + " ".join(detail_parts)

    # Build recommendations
    recs: list[str] = []
    if drop_pct > 5.0:
        recs.append(
            "HIGH DROP RATE: Switch to TCP transport (more reliable than UDP for high-loss networks). "
            "Check network switch bandwidth and camera firmware."
        )
    elif drop_pct > 1.0:
        recs.append(
            "MODERATE DROP RATE: Consider switching to TCP transport if currently using UDP. "
            "Check for network congestion between camera and recorder."
        )

    if missed_packets > 50:
        recs.append(
            "PACKET LOSS DETECTED: Check the physical network path to the camera. "
            "Look for faulty cables, overloaded switches, or Wi-Fi interference."
        )

    if startup_latency > 5.0:
        recs.append(
            "SLOW STARTUP: High startup latency may indicate the camera is slow to respond or "
            "the network path has high latency. Check camera CPU usage and network ping times."
        )

    if freeze_total > 2.0:
        recs.append(
            "STREAM FREEZES: Freezing is often caused by buffer overflows or insufficient bandwidth. "
            "Try reducing the stream resolution or bitrate, or increase buffer size."
        )

    if fps_jitter > 20.0:
        recs.append(
            "HIGH FPS JITTER: Unstable frame timing suggests the camera is struggling to encode "
            "at the nominal frame rate. Reduce resolution/bitrate or check camera load."
        )

    if bw_avg > 0 and bw_p95 > bw_avg * 2.0:
        recs.append(
            "BANDWIDTH SPIKES: The P95 bandwidth is more than 2x the average, indicating "
            "bursty traffic. Ensure sufficient network headroom for peak loads."
        )

    if warning_count > 20:
        recs.append(
            "MANY WARNINGS: A large number of log warnings were emitted. Review the Warning "
            "Samples section at the end of this report for details."
        )

    if not recs:
        recs.append(
            "No significant issues detected. Continue monitoring regularly to track trends "
            "and detect degradation early."
        )

    return summary_text, recs


def _add_per_second_detail_pages(pdf: "FPDF", report_data: dict, engine_info: dict) -> None:
    """Append per-second telemetry table pages to *pdf*."""
    timeline = report_data.get("timeline", [])
    if not timeline:
        return

    elapsed_points = [float(item.get("analysis_elapsed_sec", item.get("elapsed_sec", 0.0)) or 0.0) for item in timeline]
    frame_points = [float(item.get("frame", 0.0) or 0.0) for item in timeline]
    drop_points = [float(item.get("estimated_drop_frames", 0.0) or 0.0) for item in timeline]
    bandwidth_points = [float(item.get("bandwidth_kbps_current", 0.0) or 0.0) for item in timeline]
    health_points = [int(item.get("health_score", 0) or 0) for item in timeline]
    realtime_points = [float(item.get("fps_realtime_num", 0.0) or 0.0) for item in timeline]
    wall_points = [
        float(item.get("wall_elapsed_sec", item.get("analysis_elapsed_sec", item.get("elapsed_sec", 0.0))) or 0.0)
        for item in timeline
    ]
    frame_intervals = cumulative_to_interval(frame_points)
    drop_intervals = cumulative_to_interval(drop_points)
    engine_requested = normalize_engine_mode(str(engine_info.get("requested", "ffmpeg")))

    if engine_requested == "gstreamer":
        columns = [
            ("Sec", 18), ("Rx", 16), ("Rx/s", 20), ("Drop", 18),
            ("BW kbps", 28), ("Drift", 24), ("Health", 22),
        ]
    else:
        columns = [
            ("Sec", 18), ("Rx", 16), ("Rx/s", 20), ("Drop", 18),
            ("BW kbps", 28), ("RT FPS", 24), ("Health", 22),
        ]

    def start_table_page() -> None:
        pdf.add_page()
        pdf.set_fill_color(42, 157, 143)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, "  Per-Second Diagnostics", border=0, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(100, 110, 130)
        pdf.multi_cell(
            0, 4.5,
            "Rows sampled from engine telemetry at ~1-second intervals.  "
            "Sec=elapsed time  Rx=cumulative frames received  Rx/s=frames per sec  "
            "Drop=cumulative drops  BW=bandwidth  RT FPS=realtime fps  Health=score 0-100",
            border=0,
        )
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(220, 230, 242)
        pdf.set_text_color(30, 40, 60)
        for col_title, col_width in columns:
            pdf.cell(col_width, 6, col_title, border=1, fill=True)
        pdf.ln(6)
        pdf.set_text_color(0, 0, 0)

    start_table_page()
    previous_elapsed = 0.0
    for idx, elapsed in enumerate(elapsed_points):
        if pdf.get_y() > pdf.h - pdf.b_margin - 9:
            start_table_page()

        delta_t = elapsed - previous_elapsed if idx > 0 else elapsed
        delta_t = delta_t if delta_t > 0 else 1.0
        interval_rx = int(round(frame_intervals[idx]))
        interval_drop = int(round(drop_intervals[idx]))
        interval_rx_fps = interval_rx / delta_t if delta_t > 0 else 0.0
        bandwidth_value = bandwidth_points[idx]
        health_value = health_points[idx]

        if engine_requested == "gstreamer":
            metric_text = f"{wall_points[idx] - elapsed:+.3f}s"
        else:
            metric_text = f"{realtime_points[idx]:.2f}"

        row_values = [
            f"{elapsed:.1f}",
            str(int(round(frame_points[idx]))),
            f"{interval_rx_fps:.2f}",
            str(interval_drop),
            f"{bandwidth_value:.1f}",
            metric_text,
            str(health_value),
        ]

        # colour-code health: green good / amber fair / red poor
        if health_value >= 75:
            pdf.set_fill_color(232, 248, 237)
        elif health_value >= 55:
            pdf.set_fill_color(255, 248, 225)
        else:
            pdf.set_fill_color(255, 232, 232)

        pdf.set_font("Helvetica", "", 8)
        for (_, col_width), cell_value in zip(columns, row_values):
            pdf.cell(col_width, 5.5, cell_value, border=1, fill=(health_value < 90))
        pdf.ln(5.5)
        previous_elapsed = elapsed


def write_pdf_report(report_path: Path, report_data: dict) -> None:
    if FPDF is None:
        raise RuntimeError(
            "PDF dependency missing. Install with: python -m pip install -r requirements.txt"
        )

    # ── Extract all report fields ─────────────────────────────────────────────
    summary = report_data.get("summary", {})
    stream = report_data.get("stream_info", {})
    engine_info = report_data.get("engine", {})
    transport_info = report_data.get("transport", {})
    transport_tests = transport_info.get("tests", {})
    deep = report_data.get("deep_diagnostics", {})
    gst_probe = deep.get("gstreamer_probe", {})
    gst_runtime = deep.get("gstreamer_runtime", {})
    deep_bandwidth = deep.get("bandwidth_kbps", {})
    deep_fps = deep.get("fps_analysis", {})
    deep_drop = deep.get("drop_analysis", {})
    drop_norm = deep_drop.get("edge_normalization", {})
    deep_freeze = deep.get("freeze_analysis", {})
    deep_packet = deep.get("packet_indicators", {})
    deep_health = deep.get("health", {})
    health_score = int(deep_health.get("score", summary.get("health_score", 0)) or 0)
    health_grade = str(deep_health.get("grade", summary.get("health_grade", "N/A")))
    estimated_drops = int(summary.get("estimated_dropped_frames", 0) or 0)
    frames_received = int(summary.get("frames_received", 0) or 0)
    status = str(report_data.get("status", "unknown"))
    warning_count = int(report_data.get("warning_count", 0) or 0)
    snapshot_info = report_data.get("snapshot", {})
    snapshot_path = str(snapshot_info.get("path", "") or "")
    snapshot_error = str(snapshot_info.get("error", "") or "")
    requested_transport = str(
        transport_info.get("requested", stream.get("requested_transport", "N/A"))
    ).upper()
    selected_transport = str(
        transport_info.get("selected", stream.get("selected_transport", "N/A"))
    ).upper()
    requested_delivery = str(
        transport_info.get("requested_delivery", stream.get("requested_delivery", "N/A"))
    ).upper()
    selected_delivery = str(
        transport_info.get("selected_delivery", stream.get("selected_delivery", "N/A"))
    ).upper()
    stream_count = int(summary.get("stream_count", stream.get("stream_count", 0)) or 0)
    video_stream_count = int(summary.get("video_stream_count", stream.get("video_stream_count", 0)) or 0)
    audio_stream_count = int(summary.get("audio_stream_count", stream.get("audio_stream_count", 0)) or 0)
    drop_rate_pct = float(deep_drop.get("estimated_drop_rate_percent", 0.0) or 0.0)
    startup_latency = float(deep.get("startup_latency_sec", 0.0) or 0.0)
    freeze_total = float(deep_freeze.get("freeze_total_sec", 0.0) or 0.0)
    missed_packets = int(deep_packet.get("rtp_missed_packets", 0) or 0)
    fps_jitter_pct = float(deep_fps.get("jitter_percent_of_nominal", 0.0) or 0.0)
    bw_avg = float(deep_bandwidth.get("avg", 0.0) or 0.0)
    bw_p95 = float(deep_bandwidth.get("p95", 0.0) or 0.0)
    run_ts = str(report_data.get("started_at", datetime.now().isoformat(timespec="seconds")))
    rtsp_url = str(report_data.get("rtsp_url", ""))
    run_id = str(report_data.get("run_id", ""))
    wall_duration = float(report_data.get("wall_clock_duration_sec", 0.0) or 0.0)
    req_duration = float(report_data.get("requested_duration_sec", 0.0) or 0.0)

    # ── Colour palette ────────────────────────────────────────────────────────
    if status == "completed":
        status_color = (27, 94, 32)
    elif status == "failed":
        status_color = (183, 28, 28)
    else:
        status_color = (56, 88, 145)

    if health_score >= 90:
        health_color = (38, 166, 91)
    elif health_score >= 75:
        health_color = (67, 160, 71)
    elif health_score >= 55:
        health_color = (251, 140, 0)
    else:
        health_color = (229, 57, 53)

    drop_color = (229, 57, 53) if estimated_drops > 0 else (67, 160, 71)
    warn_color = (255, 152, 0) if warning_count > 0 else (67, 160, 71)
    DARK_BLUE = (10, 30, 60)
    ACCENT = (42, 157, 143)
    LIGHT_GREY = (240, 244, 248)
    MID_GREY = (180, 190, 200)

    # ── PDF object ────────────────────────────────────────────────────────────
    class _ReportPDF(FPDF):
        """FPDF subclass that renders a running footer on every page."""
        _report_title: str = APP_TITLE
        _run_id: str = ""
        _total_pages_placeholder: str = "{total_pages}"

        def footer(self) -> None:
            self.set_y(-11)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(140, 150, 165)
            left_text = f"{self._report_title}  |  Run: {self._run_id}"
            self.cell(0, 5, left_text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_y(-8)
            self.set_font("Helvetica", "", 7)
            self.cell(0, 5, f"Page {self.page_no()}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(0, 0, 0)

    pdf = _ReportPDF()
    pdf._run_id = run_id
    pdf.set_auto_page_break(auto=True, margin=16)

    # ── Shared helpers ────────────────────────────────────────────────────────
    def section_header(title: str, subtitle: str = "") -> None:
        """Draw a coloured section banner with optional subtitle."""
        pdf.set_fill_color(*ACCENT)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 9, f"  {title}", border=0, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        if subtitle:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(100, 110, 125)
            pdf.cell(0, 5, f"  {subtitle}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(1)

    def kv_row(label: str, value: str, label_w: float = 80, note: str = "") -> None:
        """Print one key / value row, optionally with a short explanatory note."""
        start_x = pdf.l_margin
        start_y = pdf.get_y()
        pdf.set_xy(start_x, start_y)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(60, 70, 90)
        pdf.multi_cell(label_w, 5.5, f"{label}:", border=0)
        label_bottom = pdf.get_y()
        pdf.set_xy(start_x + label_w + 2, start_y)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.multi_cell(0, 5.5, str(value), border=0)
        value_bottom = pdf.get_y()
        pdf.set_y(max(label_bottom, value_bottom))
        if note:
            pdf.set_font("Helvetica", "I", 7.5)
            pdf.set_text_color(130, 140, 155)
            pdf.set_x(start_x + label_w + 2)
            pdf.multi_cell(0, 4.5, note, border=0)
            pdf.set_text_color(0, 0, 0)

    def draw_card(x: float, y: float, w: float, h: float, title: str, value: str,
                  fill_rgb: tuple[int, int, int], sub: str = "") -> None:
        """Coloured KPI card with title, big value, and optional sub-label."""
        pdf.set_fill_color(*fill_rgb)
        pdf.rect(x, y, w, h, style="F")
        # subtle inner highlight bar
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(x, y, w, 1.5, style="F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_xy(x + 3, y + 3)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(w - 6, 5, title, ln=1)
        pdf.set_x(x + 3)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(w - 6, 9, value, ln=1)
        if sub:
            pdf.set_x(x + 3)
            pdf.set_font("Helvetica", "", 7)
            pdf.cell(w - 6, 5, sub, ln=1)
        pdf.set_text_color(0, 0, 0)

    def horizontal_rule(r: int = 200, g: int = 210, b: int = 220) -> None:
        pdf.set_draw_color(r, g, b)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_line_width(0.2)
        pdf.ln(2)

    # ── Build narrative ───────────────────────────────────────────────────────
    exec_summary, recommendations = _build_diagnosis_narrative(
        health_score=health_score,
        health_grade=health_grade,
        estimated_drops=estimated_drops,
        frames_received=frames_received,
        warning_count=warning_count,
        drop_rate=drop_rate_pct,
        startup_latency=startup_latency,
        freeze_total=freeze_total,
        missed_packets=missed_packets,
        fps_jitter=fps_jitter_pct,
        bw_avg=bw_avg,
        bw_p95=bw_p95,
        status=status,
    )

    # ══════════════════════════════════════════════════════════════════════════
    #  COVER PAGE
    # ══════════════════════════════════════════════════════════════════════════
    pdf.add_page()
    # dark header band
    pdf.set_fill_color(*DARK_BLUE)
    pdf.rect(0, 0, pdf.w, 68, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_xy(pdf.l_margin, 14)
    pdf.cell(0, 12, APP_TITLE, ln=1)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_xy(pdf.l_margin, 30)
    pdf.cell(0, 7, "CCTV / IP Camera Stream Health Report", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(pdf.l_margin, 42)
    pdf.cell(0, 6, f"Generated: {run_ts}   |   Run ID: {run_id}   |   v{APP_VERSION} ({BUILD_DATE})", ln=1)
    pdf.set_xy(pdf.l_margin, 52)
    url_display = shorten_text(rtsp_url, 90)
    pdf.cell(0, 6, f"Camera URL: {url_display}", ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_y(76)

    # ── Top-level KPI cards (row 1: 5 cards) ─────────────────────────────────
    card_gap = 4.0
    n_cards = 5
    card_w = (pdf.w - 2 * pdf.l_margin - card_gap * (n_cards - 1)) / n_cards
    y_cards = pdf.get_y()
    card_h = 30

    # Status
    draw_card(pdf.l_margin, y_cards, card_w, card_h, "RUN STATUS",
              status.upper(), status_color)
    # Health
    draw_card(pdf.l_margin + (card_w + card_gap), y_cards, card_w, card_h, "HEALTH SCORE",
              str(health_score), health_color, sub=health_grade)
    # Drop
    total_frames = max(1, frames_received + estimated_drops)
    drop_pct_display = f"{drop_rate_pct:.1f}%"
    draw_card(pdf.l_margin + (card_w + card_gap) * 2, y_cards, card_w, card_h, "FRAMES DROPPED",
              str(estimated_drops), drop_color, sub=drop_pct_display)
    # Received
    draw_card(pdf.l_margin + (card_w + card_gap) * 3, y_cards, card_w, card_h, "FRAMES RECEIVED",
              str(frames_received), (30, 100, 160))
    # Warnings
    draw_card(pdf.l_margin + (card_w + card_gap) * 4, y_cards, card_w, card_h, "WARNINGS",
              str(warning_count), warn_color)
    pdf.set_y(y_cards + card_h + 5)

    # ── Row 2: secondary KPI cards ────────────────────────────────────────────
    n2 = 4
    card_w2 = (pdf.w - 2 * pdf.l_margin - card_gap * (n2 - 1)) / n2
    y2 = pdf.get_y()
    card_h2 = 26

    bw_avg_str = f"{bw_avg:.0f} kbps" if bw_avg > 0 else "N/A"
    startup_str = f"{startup_latency:.1f}s" if startup_latency > 0 else "N/A"
    freeze_str = f"{freeze_total:.1f}s" if freeze_total > 0 else "None"
    fps_nom = float(summary.get("stream_nominal_fps", 0.0) or 0.0)
    fps_str = f"{fps_nom:.2f}" if fps_nom > 0 else "N/A"

    draw_card(pdf.l_margin, y2, card_w2, card_h2, "AVG BANDWIDTH", bw_avg_str,
              (55, 100, 155), sub=f"P95: {bw_p95:.0f} kbps" if bw_p95 > 0 else "")
    draw_card(pdf.l_margin + (card_w2 + card_gap), y2, card_w2, card_h2, "STARTUP LATENCY",
              startup_str, (100, 80, 155) if startup_latency > 3 else (67, 160, 71))
    draw_card(pdf.l_margin + (card_w2 + card_gap) * 2, y2, card_w2, card_h2, "FREEZE TIME",
              freeze_str, (229, 57, 53) if freeze_total > 0 else (67, 160, 71))
    draw_card(pdf.l_margin + (card_w2 + card_gap) * 3, y2, card_w2, card_h2, "NOMINAL FPS",
              fps_str, (30, 100, 160))
    pdf.set_y(y2 + card_h2 + 6)

    # ── Executive Summary ─────────────────────────────────────────────────────
    section_header("Executive Summary", "Plain-language diagnosis of this camera stream")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(30, 40, 55)
    pdf.multi_cell(0, 5.5, exec_summary, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # ── Recommendations ───────────────────────────────────────────────────────
    section_header("Recommendations", "Action items to improve stream health")
    for idx, rec in enumerate(recommendations, start=1):
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(60, 80, 110)
        pdf.cell(7, 5.5, f"{idx}.", border=0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 40, 55)
        pdf.multi_cell(0, 5.5, rec, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(0.5)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # ══════════════════════════════════════════════════════════════════════════
    #  PAGE 2 – TECHNICAL METRICS
    # ══════════════════════════════════════════════════════════════════════════
    pdf.add_page()
    section_header("Key Technical Metrics", "Detailed measured values from the diagnostic run")

    kv_rows: list[tuple[str, str, str]] = [
        (
            "Engine (req / validation / analytics)",
            (
                f"{engine_info.get('requested', 'ffmpeg')} / "
                f"{engine_info.get('validation_backend', 'ffprobe')} / "
                f"{engine_info.get('analytics_backend', 'ffmpeg')}"
            ),
            "The analysis engine used to capture and decode the stream.",
        ),
        ("Diagnostic Model", deep_drop.get("diagnostic_model", "N/A"),
         "Algorithm used to estimate frame drops."),
        (
            "Codec / Resolution",
            f"{stream.get('codec_name', 'N/A')} / {stream.get('width', 0)}x{stream.get('height', 0)}",
            "Video codec and resolution reported by the stream.",
        ),
        (
            "RTSP Transport (req / selected)",
            f"{requested_transport} / {selected_transport}",
            "TCP is more reliable; UDP has lower latency but is prone to loss.",
        ),
        (
            "Delivery mode (req / selected)",
            f"{requested_delivery} / {selected_delivery}",
            "Unicast = direct connection; multicast = shared group stream.",
        ),
        (
            "Streams (total / video / audio)",
            f"{stream_count} / {video_stream_count} / {audio_stream_count}",
            "",
        ),
        (
            "Duration (req / media / wall sec)",
            f"{req_duration} / {summary.get('media_elapsed_sec', 'N/A')} / {wall_duration:.1f}",
            "Wall time close to media time = real-time delivery (healthy).",
        ),
        ("Nominal FPS", str(summary.get("stream_nominal_fps", "N/A")),
         "Frame rate the camera reports in its stream metadata."),
        (
            "Frames Received / Expected",
            f"{frames_received} / {summary.get('expected_frames', 'N/A')}",
            "Expected = nominal FPS × run duration. Gap = estimated drops.",
        ),
        (
            "Estimated Drop Rate",
            f"{drop_rate_pct:.3f}%",
            ">5% is problematic; >10% severely impacts recording quality.",
        ),
        (
            "FFmpeg-reported drops / dups",
            f"{summary.get('ffmpeg_reported_dropped_frames', 'N/A')} / "
            f"{summary.get('ffmpeg_reported_duplicated_frames', 'N/A')}",
            "Cross-check from FFmpeg's own drop/duplicate counters.",
        ),
        (
            "Bandwidth (avg / min / max kbps)",
            f"{deep_bandwidth.get('avg', 'N/A')} / {deep_bandwidth.get('min', 'N/A')} / "
            f"{deep_bandwidth.get('max', 'N/A')}",
            "Higher max-to-avg ratio = more variable bitrate (common with H.264/H.265 B-frames).",
        ),
        ("Bandwidth P95 (kbps)", str(deep_bandwidth.get("p95", "N/A")),
         "95th-percentile bandwidth - size your network headroom to at least this value."),
        ("Bandwidth Source", deep_bandwidth.get("source", "N/A"), ""),
        (
            "Startup Latency (sec)",
            f"{startup_latency:.3f}",
            "Time between process start and first frame. >3s may indicate camera delay or network RTT.",
        ),
        (
            "FPS Jitter (% of nominal)",
            f"{fps_jitter_pct:.3f}%",
            "<5% is excellent; >15% indicates unstable encoding or delivery timing.",
        ),
        (
            "Realtime FPS (avg / min / max)",
            f"{deep_fps.get('realtime_fps_avg', 'N/A')} / {deep_fps.get('realtime_fps_min', 'N/A')} / "
            f"{deep_fps.get('realtime_fps_max', 'N/A')}",
            "Realtime FPS measured by the decoder. Should stay near nominal.",
        ),
        (
            "Freeze (events / total sec / ratio)",
            f"{deep_freeze.get('freeze_events_count', 'N/A')} / "
            f"{deep_freeze.get('freeze_total_sec', 'N/A')}s / "
            f"{deep_freeze.get('freeze_ratio_percent', 'N/A')}%",
            "Any freeze >0.5s per event may cause recording gaps.",
        ),
        (
            "RTP Missed Packets",
            str(deep_packet.get("rtp_missed_packets", "N/A")),
            ">0 indicates UDP packet loss. Switch to TCP transport to eliminate this.",
        ),
        (
            "GStreamer Validation",
            (
                "Not requested"
                if engine_info.get("requested", "ffmpeg") != "gstreamer"
                else (
                    f"{'OK' if gst_probe.get('ok') else 'FAILED'} / "
                    f"{Path(str(gst_probe.get('binary', 'gstreamer'))).name} / "
                    f"{gst_probe.get('transport', selected_transport).upper()}"
                )
            ),
            "",
        ),
        (
            "Drop Normalization",
            f"Startup bias removed={drop_norm.get('startup_bias_frames_removed', 0)} frames; "
            f"Duration capped={drop_norm.get('analysis_duration_capped_to_requested_duration', False)}; "
            f"Tail-end snapshot ignored={drop_norm.get('tail_end_snapshot_ignored_when_no_new_frames', False)}",
            "Edge adjustments applied to the drop estimate to remove false positives.",
        ),
    ]
    for label, value, note in kv_rows:
        kv_row(label, value, note=note)
        horizontal_rule()

    # ── Transport probe ───────────────────────────────────────────────────────
    _transport_notes = {
        "tcp": "TCP is the most reliable transport - recommended for high-quality recording.",
        "udp": "UDP has lower latency but packets may be lost on congested networks.",
        "udp_multicast": "Multicast: efficient for many viewers but may not traverse all switches.",
    }
    if transport_tests:
        pdf.ln(2)
        section_header("Transport Probe Results",
                        "Tests each RTSP transport to find the best path to the camera")
        for mode in RTSP_TRANSPORT_PROBE_CANDIDATES:
            test = transport_tests.get(mode, {})
            ok = bool(test.get("ok", False))
            scount = int(test.get("stream_count", 0) or 0)
            err = str(test.get("error", "") or "")
            status_text = "OK" if ok else "FAILED"
            detail = f"{status_text}  |  streams={scount}" if ok else f"{status_text}  |  {err or 'No details'}"
            note = _transport_notes.get(mode, "")
            kv_row(mode.upper(), detail, label_w=28, note=note)
            horizontal_rule()

    # ── GStreamer section ─────────────────────────────────────────────────────
    if engine_info.get("requested", "ffmpeg") == "gstreamer":
        pdf.ln(2)
        section_header("GStreamer Diagnostics",
                        "Details from the GStreamer pipeline used for this diagnostic run")
        packet_stats = gst_runtime.get("packet_stats", {})
        packet_text = (
            " / ".join(f"{k}={v}" for k, v in packet_stats.items() if v)
            if packet_stats
            else "No RTP counters emitted by this GStreamer runtime."
        )
        gst_pairs: list[tuple[str, str, str]] = [
            ("Status", "OK" if gst_probe.get("ok") else "FAILED", ""),
            ("Discoverer", stream.get("discovery_backend", "N/A"), ""),
            ("Launch Binary",
             Path(str(gst_runtime.get("launch_binary", gst_probe.get("binary", "gstreamer")))).name, ""),
            ("Transport", str(gst_probe.get("transport", selected_transport)).upper(), ""),
            ("Startup Check (sec)", str(gst_probe.get("startup_check_sec", "N/A")), ""),
            ("Decoder Element", gst_runtime.get("decoder_element", "auto"), ""),
            ("Device Context", gst_runtime.get("device_context", "software/auto"), ""),
            ("Bitrate Source", gst_runtime.get("bitrate_source", deep_bandwidth.get("source", "N/A")), ""),
            (
                "Wall Clock Drift (avg / max sec)",
                f"{deep_drop.get('gstreamer_wall_clock_drift_sec_avg', 0)} / "
                f"{deep_drop.get('gstreamer_wall_clock_drift_sec_max', 0)}",
                "Drift >0.5s may indicate buffering or clock skew issues.",
            ),
            ("Packet Stats", packet_text, ""),
            ("Progress Phases", ", ".join(gst_runtime.get("progress_phases", [])) or "N/A", ""),
            ("RTP Chain", ", ".join(gst_runtime.get("rtp_elements", [])) or "N/A", ""),
            ("Decode Chain", ", ".join(gst_runtime.get("decode_elements", [])) or "N/A", ""),
        ]
        gst_error = str(gst_probe.get("error", "") or "")
        if gst_error:
            gst_pairs.append(("Error", gst_error, ""))
        for label, value, note in gst_pairs:
            kv_row(label, value, label_w=44, note=note)
            horizontal_rule()

    # ── Run error ─────────────────────────────────────────────────────────────
    run_error = report_data.get("error")
    if run_error:
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(183, 28, 28)
        pdf.multi_cell(0, 6, f"Run Error: {run_error}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)

    with tempfile.TemporaryDirectory(prefix="rtsp_diag_charts_") as tmp_dir:
        chart_paths = generate_report_charts(report_data, Path(tmp_dir))
        if chart_paths:
            def place_chart(title: str, key: str, height: float, subtitle: str = "") -> None:
                chart_path = chart_paths.get(key)
                if not chart_path or not Path(chart_path).exists():
                    return
                if pdf.get_y() > pdf.h - pdf.b_margin - height - 18:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(30, 40, 60)
                pdf.cell(0, 6, title, ln=1)
                if subtitle:
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.set_text_color(100, 110, 130)
                    pdf.cell(0, 4.5, subtitle, ln=1)
                    pdf.set_text_color(0, 0, 0)
                pdf.ln(0.5)
                chart_y = pdf.get_y()
                pdf.image(chart_path, x=pdf.l_margin, y=chart_y, w=pdf.w - (2 * pdf.l_margin), h=height)
                pdf.set_y(chart_y + height + 5)

            pdf.add_page()
            section_header("Timeline Charts", "Chronological metrics captured throughout the diagnostic run")
            place_chart(
                "Live Dashboard Graph",
                "timeline_frames",
                78,
                subtitle="Green = frames captured per interval, Red = frames dropped, Blue line = bandwidth (kbps)",
            )
            place_chart(
                "Expected vs Received Frames",
                "expected_vs_received",
                76,
                subtitle="Orange shaded area = cumulative gap between expected and received frames",
            )
            place_chart(
                "Quality Timeline - FPS & Health Score",
                "timeline_performance",
                80,
                subtitle="Top panel: realtime FPS;  Bottom panel: health score with zone bands (green=excellent, red=poor)",
            )
            place_chart(
                "Drop Timeline",
                "drop_timeline",
                80,
                subtitle="Top panel: cumulative drops;  Bottom panel: drops per sample interval + instantaneous drop rate %",
            )

            if any(chart_paths.get(key) for key in ("bandwidth_distribution", "media_vs_wall", "frame_distribution", "warning_categories")):
                pdf.add_page()
                section_header("Distribution & Detail Charts", "Statistical breakdown of key metrics")
                place_chart(
                    "Bandwidth Distribution",
                    "bandwidth_distribution",
                    66,
                    subtitle="Histogram of bandwidth samples. Orange line=mean, green=median, red=P95.",
                )
                place_chart(
                    "Media Clock vs Wall Clock",
                    "media_vs_wall",
                    72,
                    subtitle="Gap between lines = clock drift. Large drift = potential buffering or sync issues.",
                )
                if chart_paths.get("frame_distribution") and Path(chart_paths["frame_distribution"]).exists():
                    if pdf.get_y() > pdf.h - pdf.b_margin - 84:
                        pdf.add_page()
                    pdf.set_font("Helvetica", "B", 10)
                    pdf.set_text_color(30, 40, 60)
                    pdf.cell(0, 6, "Frame Distribution & Warning Categories", ln=1)
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.set_text_color(100, 110, 130)
                    pdf.cell(0, 4.5, "Pie = share of received vs dropped frames;  Bar chart = warning breakdown by type", ln=1)
                    pdf.set_text_color(0, 0, 0)
                    pdf.ln(1)
                    chart_y = pdf.get_y()
                    pie_w = 84
                    pdf.image(chart_paths["frame_distribution"], x=pdf.l_margin, y=chart_y, w=pie_w)
                    warn_chart = chart_paths.get("warning_categories")
                    if warn_chart and Path(warn_chart).exists():
                        pdf.image(
                            warn_chart,
                            x=pdf.l_margin + pie_w + 6,
                            y=chart_y,
                            w=(pdf.w - 2 * pdf.l_margin - pie_w - 6),
                        )
                    pdf.set_y(chart_y + 78)
        else:
            pdf.add_page()
            section_header("Timeline Charts", "")
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(
                0,
                6,
                "Chart rendering was unavailable for this report. This usually means timeline data was missing "
                "or the chart backend was not available in the current runtime.",
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )

        if snapshot_path and Path(snapshot_path).exists():
            pdf.add_page()
            section_header("Camera Snapshot", "Live frame captured from the camera at diagnostic time")
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(100, 110, 130)
            pdf.cell(0, 5, f"Source: {snapshot_path}", ln=1)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
            max_w = pdf.w - (2 * pdf.l_margin)
            pdf.image(snapshot_path, x=pdf.l_margin, y=pdf.get_y(), w=max_w)
        elif snapshot_error:
            pdf.add_page()
            section_header("Camera Snapshot", "")
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, f"Snapshot unavailable: {snapshot_error}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    _add_per_second_detail_pages(pdf, report_data, engine_info)

    stream_inventory = stream.get("streams", [])
    if stream_inventory:
        pdf.add_page()
        section_header("Stream Inventory", "All media streams found in this RTSP source")
        pdf.set_font("Helvetica", "", 9.5)
        for stream_item in stream_inventory:
            idx = int(stream_item.get("index", 0) or 0)
            ctype = str(stream_item.get("codec_type", "unknown") or "unknown")
            codec = str(stream_item.get("codec_name", "unknown") or "unknown")
            bitrate = float(stream_item.get("bit_rate_kbps", 0.0) or 0.0)
            line = f"#{idx}  [{ctype.upper()}]  {codec.upper()}"
            if ctype == "video":
                line += (
                    f"  |  {stream_item.get('width', 0)}x{stream_item.get('height', 0)}"
                    f"  |  {stream_item.get('fps', 0.0)} FPS"
                )
            elif ctype == "audio":
                line += (
                    f"  |  {stream_item.get('sample_rate_hz', 0)} Hz"
                    f"  |  ch={stream_item.get('channels', 0)}"
                )
            if bitrate > 0:
                line += f"  |  {bitrate:.1f} kbps"
            pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            horizontal_rule()

    warning_samples = report_data.get("warning_samples", [])
    if warning_samples:
        pdf.add_page()
        section_header("Warning Samples", "Raw warning/error log lines captured during the run")
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(120, 130, 145)
        pdf.multi_cell(
            0, 4.5,
            "These are the first warnings logged during the run. Use them to diagnose camera, "
            "codec, or network-level errors.",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)
        pdf.set_font("Helvetica", "", 8)
        for idx, warning_line in enumerate(warning_samples, start=1):
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5, f"{idx}.  {warning_line}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.output(str(report_path))


class LiveChartCard(ttk.LabelFrame):
    def __init__(self, parent: tk.Misc, title: str, line_color: str, fill_color: str):
        super().__init__(parent, text=title, padding=8)
        self.line_color = line_color
        self.fill_color = fill_color
        self.value_var = tk.StringVar(value="Waiting for data")
        self.samples: list[float] = []

        header = ttk.Frame(self)
        header.pack(fill=tk.X)
        ttk.Label(header, textvariable=self.value_var).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(
            self,
            height=110,
            bg="#0D1B2A",
            highlightthickness=1,
            highlightbackground="#30475E",
        )
        self.canvas.pack(fill=tk.X, expand=True, pady=(6, 0))
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

    def clear(self, message: str = "Waiting for data") -> None:
        self.samples = []
        self.value_var.set(message)
        self.redraw()

    def update_series(self, samples: list[float], value_text: str) -> None:
        self.samples = [max(0.0, float(value)) for value in samples[-LIVE_CHART_MAX_POINTS:]]
        self.value_var.set(value_text)
        self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        width = max(int(self.canvas.winfo_width()), 40)
        height = max(int(self.canvas.winfo_height()), 60)

        for step in range(1, 4):
            y = int((height - 16) * step / 4)
            self.canvas.create_line(8, y, width - 8, y, fill="#1B263B")

        if len(self.samples) < 2:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text="Live data appears here during diagnostics",
                fill="#A9BCD0",
                font=("Segoe UI", 9),
            )
            return

        min_value = min(self.samples)
        max_value = max(self.samples)
        if max_value <= min_value:
            max_value = min_value + 1.0

        x_span = max(width - 16, 1)
        y_span = max(height - 20, 1)
        points: list[float] = []
        fill_points: list[float] = [8.0, float(height - 10)]
        count = len(self.samples) - 1

        for idx, value in enumerate(self.samples):
            x = 8.0 + (x_span * idx / max(count, 1))
            normalized = (value - min_value) / (max_value - min_value)
            y = 8.0 + (y_span * (1.0 - normalized))
            points.extend([x, y])
            fill_points.extend([x, y])

        fill_points.extend([points[-2], float(height - 10)])
        self.canvas.create_polygon(*fill_points, fill=self.fill_color, outline="")
        self.canvas.create_line(*points, fill=self.line_color, width=2, smooth=True)
        self.canvas.create_text(
            width - 10,
            10,
            text=f"{max_value:.1f}",
            fill="#E0E1DD",
            anchor="ne",
            font=("Segoe UI", 8),
        )
        self.canvas.create_text(
            width - 10,
            height - 8,
            text=f"{min_value:.1f}",
            fill="#E0E1DD",
            anchor="se",
            font=("Segoe UI", 8),
        )


class CombinedLiveChartCard(ttk.LabelFrame):
    def __init__(self, parent: tk.Misc, title: str):
        super().__init__(parent, text=title, padding=8)
        self.value_var = tk.StringVar(value="Waiting for data")
        self.received_samples: list[float] = []
        self.dropped_samples: list[float] = []
        self.bandwidth_samples: list[float] = []

        header = ttk.Frame(self)
        header.pack(fill=tk.X)
        ttk.Label(header, textvariable=self.value_var).pack(side=tk.RIGHT)

        body = ttk.Frame(self)
        body.pack(fill=tk.X, expand=True, pady=(6, 0))

        self.canvas = tk.Canvas(
            body,
            height=170,
            bg="#0D1B2A",
            highlightthickness=1,
            highlightbackground="#30475E",
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        legend = ttk.Frame(body)
        legend.pack(side=tk.RIGHT, fill=tk.Y, padx=(12, 0))
        for color, label in (
            ("#80ED99", "Frames Received"),
            ("#F94144", "Dropped Frames"),
            ("#4CC9F0", "Bandwidth"),
        ):
            row = ttk.Frame(legend)
            row.pack(anchor="nw", pady=4)
            swatch = tk.Label(row, bg=color, width=2, height=1, relief="flat")
            swatch.pack(side=tk.LEFT, padx=(0, 6))
            ttk.Label(row, text=label).pack(side=tk.LEFT)

    def clear(self, message: str = "Waiting for data") -> None:
        self.received_samples = []
        self.dropped_samples = []
        self.bandwidth_samples = []
        self.value_var.set(message)
        self.redraw()

    def update_series(
        self,
        received_samples: list[float],
        dropped_samples: list[float],
        bandwidth_samples: list[float],
        value_text: str,
    ) -> None:
        self.received_samples = [max(0.0, float(value)) for value in received_samples[-LIVE_CHART_MAX_POINTS:]]
        self.dropped_samples = [max(0.0, float(value)) for value in dropped_samples[-LIVE_CHART_MAX_POINTS:]]
        self.bandwidth_samples = [max(0.0, float(value)) for value in bandwidth_samples[-LIVE_CHART_MAX_POINTS:]]
        self.value_var.set(value_text)
        self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        width = max(int(self.canvas.winfo_width()), 180)
        height = max(int(self.canvas.winfo_height()), 120)
        left_pad = 42
        right_pad = 42
        top_pad = 14
        bottom_pad = 20

        plot_left = left_pad
        plot_right = width - right_pad
        plot_top = top_pad
        plot_bottom = height - bottom_pad
        plot_width = max(plot_right - plot_left, 1)
        plot_height = max(plot_bottom - plot_top, 1)

        for step in range(5):
            y = plot_top + (plot_height * step / 4)
            self.canvas.create_line(plot_left, y, plot_right, y, fill="#1B263B")

        if len(self.bandwidth_samples) < 2 or len(self.received_samples) < 2 or len(self.dropped_samples) < 2:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text="Live data appears here during diagnostics",
                fill="#A9BCD0",
                font=("Segoe UI", 9),
            )
            return

        sample_indices = select_chart_indices(len(self.bandwidth_samples), 48)
        received_plot = [self.received_samples[index] for index in sample_indices]
        dropped_plot = [self.dropped_samples[index] for index in sample_indices]
        bandwidth_plot = [self.bandwidth_samples[index] for index in sample_indices]
        received_interval_plot = cumulative_to_interval(received_plot)
        dropped_interval_plot = cumulative_to_interval(dropped_plot)

        max_frames = max(received_interval_plot + dropped_interval_plot + [1.0])
        max_bandwidth = max(bandwidth_plot + [1.0])
        count = len(sample_indices)
        x_step = plot_width / max(count, 1)
        group_width = max(6.0, min(18.0, x_step * 0.72))
        bar_half = max(group_width * 0.22, 2.0)
        line_points: list[float] = []

        self.canvas.create_line(plot_left, plot_top, plot_left, plot_bottom, fill="#415A77")
        self.canvas.create_line(plot_right, plot_top, plot_right, plot_bottom, fill="#415A77")
        self.canvas.create_line(plot_left, plot_bottom, plot_right, plot_bottom, fill="#415A77")

        for idx in range(count):
            center_x = plot_left + ((idx + 0.5) * plot_width / max(count, 1))

            received_height = (received_interval_plot[idx] / max_frames) * plot_height if max_frames > 0 else 0.0
            dropped_height = (dropped_interval_plot[idx] / max_frames) * plot_height if max_frames > 0 else 0.0
            bandwidth_y = plot_bottom - ((bandwidth_plot[idx] / max_bandwidth) * plot_height if max_bandwidth > 0 else 0.0)

            self.canvas.create_rectangle(
                center_x - (bar_half * 2),
                plot_bottom - received_height,
                center_x - 2,
                plot_bottom,
                fill="#80ED99",
                outline="",
            )
            self.canvas.create_rectangle(
                center_x + 2,
                plot_bottom - dropped_height,
                center_x + (bar_half * 2),
                plot_bottom,
                fill="#F94144",
                outline="",
            )
            line_points.extend([center_x, bandwidth_y])

        if len(line_points) >= 4:
            self.canvas.create_line(*line_points, fill="#4CC9F0", width=2, smooth=True)

        self.canvas.create_text(
            6,
            plot_top,
            text=f"{max_frames:.0f}",
            fill="#E0E1DD",
            anchor="nw",
            font=("Segoe UI", 8),
        )
        self.canvas.create_text(
            6,
            plot_bottom,
            text="0",
            fill="#E0E1DD",
            anchor="sw",
            font=("Segoe UI", 8),
        )
        self.canvas.create_text(
            width - 6,
            plot_top,
            text=f"{max_bandwidth:.0f} kbps",
            fill="#E0E1DD",
            anchor="ne",
            font=("Segoe UI", 8),
        )
        self.canvas.create_text(
            width - 6,
            plot_bottom,
            text="0 kbps",
            fill="#E0E1DD",
            anchor="se",
            font=("Segoe UI", 8),
        )


class DiagnosticApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE}  v{APP_VERSION}  (Build {BUILD_DATE})")
        self.root.geometry(self._initial_window_geometry())
        self.root.minsize(860, 620)
        self._icon_img = None

        self.base_dir = get_app_base_dir()
        self.reports_dir = self._resolve_default_reports_dir()

        self.event_queue: queue.Queue = queue.Queue()
        self.worker: Optional[DiagnosticWorker] = None
        self.test_running = False

        self.ffmpeg_path = detect_ff_binary("ffmpeg")
        self.ffprobe_path = detect_ff_binary("ffprobe")
        self.ffplay_path = detect_ff_binary("ffplay")
        self.gst_launch_path = detect_gst_binary("gst-launch-1.0")
        self.gst_play_path = detect_gst_binary("gst-play-1.0")
        self.gst_discoverer_path = detect_gst_binary("gst-discoverer-1.0")
        self.ffplay_process: Optional[subprocess.Popen] = None
        self.preview_started_by_test = False

        self.rtsp_var = tk.StringVar()
        self.duration_var = tk.StringVar(value="60")
        self.engine_var = tk.StringVar(value="ffmpeg")
        self.transport_mode_var = tk.StringVar(value="auto")
        self.status_var = tk.StringVar(value="Idle")
        self.report_dir_var = tk.StringVar(value=str(self.reports_dir))
        self.ffmpeg_var = tk.StringVar(value=self.ffmpeg_path or "Not found")
        self.ffprobe_var = tk.StringVar(value=self.ffprobe_path or "Not found (FFmpeg fallback mode)")
        self.ffplay_var = tk.StringVar(value=self.ffplay_path or "Not found")
        self.gstreamer_var = tk.StringVar(
            value=self.gst_launch_path or self.gst_discoverer_path or self.gst_play_path or "Not found"
        )
        self.auto_preview_var = tk.BooleanVar(value=False)

        self.stream_var = tk.StringVar(value="N/A")
        self.transport_live_var = tk.StringVar(value="N/A")
        self.stream_counts_var = tk.StringVar(value="0/0/0")
        self.elapsed_var = tk.StringVar(value="0.0 s")
        self.received_var = tk.StringVar(value="0")
        self.expected_var = tk.StringVar(value="0")
        self.estimated_drops_var = tk.StringVar(value="0")
        self.ffmpeg_drops_var = tk.StringVar(value="0")
        self.ffmpeg_dups_var = tk.StringVar(value="0")
        self.warning_count_var = tk.StringVar(value="0")
        self.bandwidth_now_var = tk.StringVar(value="0 kbps")
        self.bandwidth_avg_var = tk.StringVar(value="0 kbps")
        self.jitter_var = tk.StringVar(value="0 %")
        self.startup_latency_var = tk.StringVar(value="0.0 s")
        self.packet_missed_var = tk.StringVar(value="0")
        self.health_var = tk.StringVar(value="100 (N/A)")
        self.last_report_var = tk.StringVar(value="-")
        self.bandwidth_chart_points: list[float] = []
        self.received_chart_points: list[float] = []
        self.dropped_chart_points: list[float] = []

        self._set_window_icon()
        self._build_ui()
        self._apply_theme()
        self._refresh_engine_ui()
        self.root.after(200, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _initial_window_geometry(self) -> str:
        screen_w = max(int(self.root.winfo_screenwidth()), 1024)
        screen_h = max(int(self.root.winfo_screenheight()), 768)
        width = min(1320, max(980, int(screen_w * 0.9)))
        height = min(940, max(720, int(screen_h * 0.86)))
        pos_x = max(0, int((screen_w - width) / 2))
        pos_y = max(0, int((screen_h - height) / 3))
        return f"{width}x{height}+{pos_x}+{pos_y}"

    def _resolve_default_reports_dir(self) -> Path:
        candidates: list[Path] = [self.base_dir / "reports"]

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "RTSP-Camera-Diagnostic" / "reports")

        docs_dir = Path.home() / "Documents"
        candidates.append(docs_dir / "RTSP-Camera-Diagnostic-Reports")
        candidates.append(Path.cwd() / "reports")

        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                # Write probe to validate permissions and avoid runtime save failures.
                probe_file = candidate / ".write_test.tmp"
                probe_file.write_text("ok", encoding="utf-8")
                probe_file.unlink(missing_ok=True)
                return candidate
            except Exception:
                continue

        return self.base_dir

    def _on_wrapper_configure(self, _event: tk.Event) -> None:
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.main_canvas.itemconfigure(self.wrapper_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not self.main_canvas.winfo_exists():
            return
        delta = int(-1 * (event.delta / 120)) if getattr(event, "delta", 0) else 0
        if delta != 0:
            self.main_canvas.yview_scroll(delta, "units")

    def _has_gstreamer_preview_backend(self) -> bool:
        return bool(self.gst_play_path or self.gst_launch_path)

    def _refresh_engine_ui(self) -> None:
        engine = normalize_engine_mode(self.engine_var.get())
        if engine == "gstreamer":
            self.preview_btn.configure(text="Start GStreamer Preview")
        else:
            self.preview_btn.configure(text="Start FFmpeg Preview")

        if engine == "ffmpeg":
            preview_available = bool(self.ffplay_path)
        else:
            preview_available = self._has_gstreamer_preview_backend()

        if preview_available:
            self.preview_btn.configure(state=tk.NORMAL)
            self.auto_preview_chk.configure(state=tk.NORMAL)
        else:
            self.preview_btn.configure(state=tk.DISABLED)
            self.auto_preview_chk.configure(state=tk.DISABLED)
            self.auto_preview_var.set(False)

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", padding=(10, 6))
        style.configure("TLabelframe.Label", font=("Segoe UI Semibold", 10))

    def _set_window_icon(self) -> None:
        ico_path = get_resource_path("assets/camera_icon.ico")
        png_path = get_resource_path("assets/camera_icon.png")
        try:
            if ico_path.exists():
                self.root.iconbitmap(default=str(ico_path))
        except Exception:
            pass
        try:
            if png_path.exists():
                self._icon_img = tk.PhotoImage(file=str(png_path))
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root)
        shell.pack(fill=tk.BOTH, expand=True)

        self.main_canvas = tk.Canvas(shell, highlightthickness=0)
        self.main_scrollbar = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=self.main_canvas.yview)
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)
        self.main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.main_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        wrapper = ttk.Frame(self.main_canvas, padding=10)
        self.wrapper_window = self.main_canvas.create_window((0, 0), window=wrapper, anchor="nw")
        wrapper.bind("<Configure>", self._on_wrapper_configure)
        self.main_canvas.bind("<Configure>", self._on_canvas_configure)
        self.main_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        header = ttk.Label(
            wrapper,
            text="RTSP Camera Frame Drop Diagnostic",
            font=("Segoe UI Semibold", 14),
        )
        header.pack(anchor="w", pady=(0, 2))

        version_label = ttk.Label(
            wrapper,
            text=f"v{APP_VERSION}  —  Build {BUILD_DATE}",
            font=("Segoe UI", 9),
        )
        version_label.pack(anchor="w", pady=(0, 8))

        input_frame = ttk.LabelFrame(wrapper, text="Test Input", padding=10)
        input_frame.pack(fill=tk.X)

        ttk.Label(input_frame, text="RTSP URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(input_frame, textvariable=self.rtsp_var, width=100).grid(
            row=0, column=1, columnspan=5, sticky="ew", padx=(8, 0)
        )

        ttk.Label(input_frame, text="Duration (sec)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.duration_var, width=14).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        ttk.Label(input_frame, text="RTSP Transport").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Combobox(
            input_frame,
            textvariable=self.transport_mode_var,
            values=["auto", "tcp", "udp", "udp_multicast"],
            state="readonly",
            width=16,
        ).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(input_frame, text="Engine").grid(row=1, column=4, sticky="w", pady=(8, 0))
        self.engine_combo = ttk.Combobox(
            input_frame,
            textvariable=self.engine_var,
            values=list(DIAGNOSTIC_ENGINES),
            state="readonly",
            width=14,
        )
        self.engine_combo.grid(row=1, column=5, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_engine_ui())

        ttk.Label(input_frame, text="Reports Folder").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.report_dir_var, width=65).grid(
            row=2, column=1, columnspan=4, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        ttk.Button(input_frame, text="Browse", command=self.choose_report_dir).grid(
            row=2, column=5, sticky="e", padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(input_frame, text="FFmpeg").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.ffmpeg_var, width=95, state="readonly").grid(
            row=3, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        ttk.Label(input_frame, text="FFprobe").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.ffprobe_var, width=95, state="readonly").grid(
            row=4, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        ttk.Label(input_frame, text="FFplay").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.ffplay_var, width=95, state="readonly").grid(
            row=5, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        ttk.Label(input_frame, text="GStreamer").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.gstreamer_var, width=95, state="readonly").grid(
            row=6, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        self.auto_preview_chk = ttk.Checkbutton(
            input_frame,
            text="Auto-start live preview during test",
            variable=self.auto_preview_var,
        )
        self.auto_preview_chk.grid(row=7, column=1, columnspan=5, sticky="w", pady=(8, 0))

        for col in (1, 2, 3, 4, 5):
            input_frame.columnconfigure(col, weight=1)

        button_frame = ttk.Frame(wrapper, padding=(0, 10, 0, 10))
        button_frame.pack(fill=tk.X)
        self.check_btn = ttk.Button(button_frame, text="Check Connection", command=self.check_connection)
        self.preview_btn = ttk.Button(button_frame, text="Start Live Preview", command=self.toggle_preview)
        self.start_btn = ttk.Button(button_frame, text="Start Diagnostic", command=self.start_test)
        self.stop_btn = ttk.Button(button_frame, text="Stop", command=self.stop_test, state=tk.DISABLED)
        self.open_reports_btn = ttk.Button(button_frame, text="Open Reports Folder", command=self.open_reports_folder)
        self.open_last_btn = ttk.Button(button_frame, text="Open Last Report", command=self.open_last_report)
        self.check_btn.pack(side=tk.LEFT)
        self.preview_btn.pack(side=tk.LEFT, padx=8)
        self.start_btn.pack(side=tk.LEFT, padx=8)
        self.stop_btn.pack(side=tk.LEFT, padx=8)
        self.open_reports_btn.pack(side=tk.LEFT, padx=8)
        self.open_last_btn.pack(side=tk.LEFT)

        summary_frame = ttk.LabelFrame(wrapper, text="Live Report", padding=10)
        summary_frame.pack(fill=tk.X)

        self._pair(summary_frame, "Status", self.status_var, 0, 0)
        self._pair(summary_frame, "Transport", self.transport_live_var, 0, 2)
        self._pair(summary_frame, "Stream", self.stream_var, 1, 0)
        self._pair(summary_frame, "Streams (T/V/A)", self.stream_counts_var, 1, 2)
        self._pair(summary_frame, "Elapsed", self.elapsed_var, 2, 0)
        self._pair(summary_frame, "Frames Received", self.received_var, 2, 2)
        self._pair(summary_frame, "Expected Frames", self.expected_var, 3, 0)
        self._pair(summary_frame, "Estimated Drops", self.estimated_drops_var, 3, 2)
        self._pair(summary_frame, "FFmpeg Drops", self.ffmpeg_drops_var, 4, 0)
        self._pair(summary_frame, "FFmpeg Duplicates", self.ffmpeg_dups_var, 4, 2)
        self._pair(summary_frame, "Warning Count", self.warning_count_var, 5, 0)
        self._pair(summary_frame, "Last Report", self.last_report_var, 5, 2)
        self._pair(summary_frame, "Bandwidth Now", self.bandwidth_now_var, 6, 0)
        self._pair(summary_frame, "Bandwidth Avg", self.bandwidth_avg_var, 6, 2)
        self._pair(summary_frame, "FPS Jitter", self.jitter_var, 7, 0)
        self._pair(summary_frame, "Startup Latency", self.startup_latency_var, 7, 2)
        self._pair(summary_frame, "Missed Packets", self.packet_missed_var, 8, 0)
        self._pair(summary_frame, "Health", self.health_var, 8, 2)

        summary_frame.columnconfigure(1, weight=1)
        summary_frame.columnconfigure(3, weight=1)

        ttk.Label(wrapper, text="Progress").pack(anchor="w")
        self.progress = ttk.Progressbar(wrapper, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(0, 10))

        charts_frame = ttk.LabelFrame(wrapper, text="Live Charts", padding=10)
        charts_frame.pack(fill=tk.X, pady=(0, 10))
        self.activity_chart = CombinedLiveChartCard(charts_frame, "Frames + Bandwidth")
        self.activity_chart.pack(fill=tk.X)

        logs_frame = ttk.LabelFrame(wrapper, text="FFmpeg Live Logs", padding=10)
        logs_frame.pack(fill=tk.BOTH, expand=True)
        self.logs = ScrolledText(logs_frame, height=14, wrap=tk.WORD, font=("Consolas", 10))
        self.logs.pack(fill=tk.BOTH, expand=True)

    def _pair(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, col: int) -> None:
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=col, sticky="w", padx=(0, 8), pady=2)
        ttk.Label(parent, textvariable=variable).grid(row=row, column=col + 1, sticky="w", pady=2)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.insert(tk.END, f"[{timestamp}] {message}\n")
        self.logs.see(tk.END)

    def _trim_chart_points(self) -> None:
        self.bandwidth_chart_points = self.bandwidth_chart_points[-LIVE_CHART_MAX_POINTS:]
        self.received_chart_points = self.received_chart_points[-LIVE_CHART_MAX_POINTS:]
        self.dropped_chart_points = self.dropped_chart_points[-LIVE_CHART_MAX_POINTS:]

    def _refresh_live_charts(self) -> None:
        self._trim_chart_points()
        latest_received = int(self.received_chart_points[-1]) if self.received_chart_points else 0
        latest_dropped = int(self.dropped_chart_points[-1]) if self.dropped_chart_points else 0
        latest_bandwidth = self.bandwidth_chart_points[-1] if self.bandwidth_chart_points else 0.0
        self.activity_chart.update_series(
            self.received_chart_points,
            self.dropped_chart_points,
            self.bandwidth_chart_points,
            f"Rx {latest_received} | Drop {latest_dropped} | BW {latest_bandwidth:.1f} kbps",
        )

    def choose_report_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.report_dir_var.get() or str(self.base_dir))
        if selected:
            try:
                test_dir = Path(selected)
                test_dir.mkdir(parents=True, exist_ok=True)
                probe_file = test_dir / ".write_test.tmp"
                probe_file.write_text("ok", encoding="utf-8")
                probe_file.unlink(missing_ok=True)
            except Exception as exc:
                messagebox.showerror("Folder not writable", f"Cannot write to selected folder:\n{exc}")
                return
            self.report_dir_var.set(selected)

    def open_reports_folder(self) -> None:
        report_dir = Path(self.report_dir_var.get())
        report_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(report_dir))

    def open_last_report(self) -> None:
        report_name = self.last_report_var.get().strip()
        if not report_name or report_name == "-":
            messagebox.showinfo("No report yet", "Run a diagnostic first to generate a report.")
            return
        report_dir = Path(self.report_dir_var.get())
        report_path = report_dir / report_name
        if not report_path.exists():
            messagebox.showwarning("File missing", f"Report not found:\n{report_path}")
            return
        os.startfile(str(report_path))

    def is_preview_running(self) -> bool:
        return bool(self.ffplay_process and self.ffplay_process.poll() is None)

    def _sync_preview_state(self) -> None:
        if self.ffplay_process and self.ffplay_process.poll() is not None:
            self.ffplay_process = None
            self._refresh_engine_ui()
            self.preview_started_by_test = False

    def _start_ffmpeg_preview(self, rtsp_url: str, transport: str) -> bool:
        if not self.ffplay_path:
            messagebox.showerror("FFplay missing", "FFplay binary was not detected.")
            return False

        ffplay_cmd = [
            self.ffplay_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-framedrop",
            "-rtsp_transport",
            transport,
            "-window_title",
            "RTSP Live Preview",
            "-i",
            rtsp_url,
        ]
        try:
            self.ffplay_process = subprocess.Popen(
                ffplay_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            messagebox.showerror("FFplay start failed", str(exc))
            return False

        self.preview_btn.configure(text="Stop Live Preview")
        self.log(f"FFmpeg preview started over {transport.upper()} for {shorten_text(rtsp_url)}")
        return True

    def _start_gstreamer_preview(self, rtsp_url: str, transport: str) -> bool:
        gst_protocol = {
            "tcp": "tcp",
            "udp": "udp",
            "udp_multicast": "udp-mcast",
        }.get(transport, "tcp")
        gst_binary = self.gst_launch_path or self.gst_play_path
        gst_env = build_gstreamer_env(gst_binary)

        if self.gst_launch_path:
            preview_cmd = [
                self.gst_launch_path,
                "-e",
                "playbin",
                f"uri={rtsp_url}",
                f"source::protocols={gst_protocol}",
                "source::latency=0",
            ]
        elif self.gst_play_path:
            preview_cmd = [self.gst_play_path, "--no-interactive", rtsp_url]
        else:
            messagebox.showerror("GStreamer missing", "No GStreamer preview binary was detected.")
            return False

        try:
            self.ffplay_process = subprocess.Popen(
                preview_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=gst_env,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            messagebox.showerror("GStreamer start failed", str(exc))
            return False

        self.preview_btn.configure(text="Stop Live Preview")
        self.log(
            f"GStreamer preview started for {shorten_text(rtsp_url)} using {transport.upper()} "
            f"({Path(gst_binary).parent})."
        )
        return True

    def start_preview(self) -> bool:
        self._sync_preview_state()
        if self.is_preview_running():
            return True
        rtsp_input = self.rtsp_var.get().strip()
        if not rtsp_input:
            messagebox.showerror("Invalid input", "RTSP URL is required.")
            return False
        rtsp_url = normalize_rtsp_url(rtsp_input)
        transport = resolve_transport_for_ff_tools(self.transport_mode_var.get())
        engine = normalize_engine_mode(self.engine_var.get())
        if engine == "gstreamer":
            return self._start_gstreamer_preview(rtsp_url, transport)
        return self._start_ffmpeg_preview(rtsp_url, transport)

    def stop_preview(self) -> None:
        if not self.ffplay_process:
            self._refresh_engine_ui()
            self.preview_started_by_test = False
            return
        if self.ffplay_process.poll() is None:
            try:
                self.ffplay_process.terminate()
                self.ffplay_process.wait(timeout=3)
            except Exception:
                try:
                    self.ffplay_process.kill()
                except Exception:
                    pass
        self.ffplay_process = None
        self._refresh_engine_ui()
        self.preview_started_by_test = False
        self.log("Live preview stopped.")

    def toggle_preview(self) -> None:
        if self.is_preview_running():
            self.stop_preview()
            return
        self.start_preview()

    def check_connection(self) -> None:
        if self.test_running:
            return
        engine = normalize_engine_mode(self.engine_var.get())
        if engine == "ffmpeg" and not self.ffmpeg_path:
            messagebox.showerror("FFmpeg missing", "FFmpeg binary was not detected.")
            return
        if engine == "gstreamer" and not self.gst_launch_path:
            messagebox.showerror("GStreamer missing", "`gst-launch-1.0` was not detected.")
            return
        rtsp_input = self.rtsp_var.get().strip()
        if not rtsp_input:
            messagebox.showerror("Invalid input", "RTSP URL is required.")
            return
        rtsp_url = normalize_rtsp_url(rtsp_input)
        requested_transport = normalize_transport_mode(self.transport_mode_var.get())
        selected_transport = resolve_transport_for_ff_tools(requested_transport)

        def _run_probe() -> None:
            self.event_queue.put(
                {
                    "type": "status",
                    "message": (
                        f"Checking RTSP connection using {engine.upper()} / "
                        f"{requested_transport.upper()}..."
                    ),
                }
            )
            try:
                if engine == "gstreamer":
                    info = probe_stream_gstreamer(
                        gst_launch_path=self.gst_launch_path,
                        gst_play_path=self.gst_play_path,
                        gst_discoverer_path=self.gst_discoverer_path,
                        rtsp_url=rtsp_url,
                        requested_transport=requested_transport,
                    )
                    selected_transport_actual = str(info.get("selected_transport", selected_transport)).upper()
                    self.event_queue.put(
                        {
                            "type": "log",
                            "line": (
                                "GStreamer connection probe passed over "
                                f"{selected_transport_actual}."
                            ),
                        }
                    )
                elif self.ffprobe_path:
                    info = probe_stream(
                        self.ffprobe_path,
                        rtsp_url,
                        requested_transport=requested_transport,
                    )
                elif self.ffmpeg_path:
                    with tempfile.TemporaryDirectory(prefix="rtsp_check_") as tmp:
                        test_path = Path(tmp) / "probe_snapshot.jpg"
                        ok, err = capture_rtsp_snapshot(
                            ffmpeg_path=self.ffmpeg_path,
                            rtsp_url=rtsp_url,
                            output_path=test_path,
                            transport=selected_transport,
                            timeout_sec=15,
                        )
                        if not ok:
                            raise RuntimeError(err or "FFmpeg fallback probe failed.")
                    info = {
                        "selected_transport": selected_transport,
                        "requested_transport": requested_transport,
                        "selected_delivery": transport_delivery_label(selected_transport),
                        "requested_delivery": transport_delivery_label(requested_transport),
                        "transport_diagnostics": {
                            "requested": requested_transport,
                            "selected": selected_transport,
                            "tests": {},
                        },
                        "stream_count": 0,
                        "video_stream_count": 0,
                        "audio_stream_count": 0,
                        "first_video_index": 0,
                        "format_bit_rate_bps": 0,
                        "format_bit_rate_kbps": 0.0,
                        "streams": [],
                        "codec_name": "unknown (ffprobe missing)",
                        "width": 0,
                        "height": 0,
                        "pix_fmt": "unknown",
                        "avg_frame_rate_raw": "0/0",
                        "r_frame_rate_raw": "0/0",
                        "fps": 0.0,
                        "bit_rate_kbps": 0.0,
                    }
                else:
                    info = {
                        "selected_transport": selected_transport,
                        "requested_transport": requested_transport,
                        "selected_delivery": transport_delivery_label(selected_transport),
                        "requested_delivery": transport_delivery_label(requested_transport),
                        "transport_diagnostics": {
                            "requested": requested_transport,
                            "selected": selected_transport,
                            "tests": {},
                        },
                        "stream_count": 0,
                        "video_stream_count": 0,
                        "audio_stream_count": 0,
                        "first_video_index": 0,
                        "format_bit_rate_bps": 0,
                        "format_bit_rate_kbps": 0.0,
                        "streams": [],
                        "codec_name": "unknown (metadata tool missing)",
                        "width": 0,
                        "height": 0,
                        "pix_fmt": "unknown",
                        "avg_frame_rate_raw": "0/0",
                        "r_frame_rate_raw": "0/0",
                        "fps": 0.0,
                        "bit_rate_kbps": 0.0,
                    }
                self.event_queue.put({"type": "stream_info", "info": info})
                self.event_queue.put({"type": "status", "message": "Connection check successful."})
            except Exception as exc:
                self.event_queue.put({"type": "status", "message": "Connection check failed."})
                self.event_queue.put({"type": "log", "line": f"Connection check failed: {exc}"})

        threading.Thread(target=_run_probe, daemon=True).start()

    def reset_live_metrics(self) -> None:
        self.stream_var.set("N/A")
        self.transport_live_var.set("N/A")
        self.stream_counts_var.set("0/0/0")
        self.elapsed_var.set("0.0 s")
        self.received_var.set("0")
        self.expected_var.set("0")
        self.estimated_drops_var.set("0")
        self.ffmpeg_drops_var.set("0")
        self.ffmpeg_dups_var.set("0")
        self.warning_count_var.set("0")
        self.bandwidth_now_var.set("0 kbps")
        self.bandwidth_avg_var.set("0 kbps")
        self.jitter_var.set("0 %")
        self.startup_latency_var.set("0.0 s")
        self.packet_missed_var.set("0")
        self.health_var.set("100 (N/A)")
        self.progress["value"] = 0
        self.bandwidth_chart_points = []
        self.received_chart_points = []
        self.dropped_chart_points = []
        self.activity_chart.clear()

    def start_test(self) -> None:
        if self.test_running:
            return

        rtsp_input = self.rtsp_var.get().strip()
        if not rtsp_input:
            messagebox.showerror("Invalid input", "RTSP URL is required.")
            return
        rtsp_url = normalize_rtsp_url(rtsp_input)
        engine_mode = normalize_engine_mode(self.engine_var.get())
        transport_mode = normalize_transport_mode(self.transport_mode_var.get())
        if engine_mode == "ffmpeg" and not self.ffmpeg_path:
            messagebox.showerror(
                "FFmpeg missing",
                "FFmpeg not found.\n"
                "Install FFmpeg and restart the app, or add binaries to C:\\ffmpeg\\bin.",
            )
            return
        if engine_mode == "gstreamer" and not self.gst_launch_path:
            messagebox.showerror(
                "GStreamer missing",
                "GStreamer engine was selected, but `gst-launch-1.0` was not detected.",
            )
            return

        try:
            duration_seconds = int(self.duration_var.get().strip())
            if duration_seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid input", "Duration must be a positive integer.")
            return

        report_dir = Path(self.report_dir_var.get().strip() or str(self.reports_dir))
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            probe_file = report_dir / ".write_test.tmp"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
        except Exception as exc:
            messagebox.showerror(
                "Report folder error",
                f"Cannot write to report folder:\n{report_dir}\n\n{exc}",
            )
            return
        self.report_dir_var.set(str(report_dir))
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.reset_live_metrics()
        self.logs.delete("1.0", tk.END)
        self.status_var.set("Starting")
        self.preview_started_by_test = False
        self.transport_live_var.set(
            f"{transport_mode.upper()}/{transport_delivery_label(transport_mode).upper()}"
        )
        if rtsp_url != rtsp_input:
            self.log("RTSP credentials were URL-encoded automatically for compatibility.")
        if engine_mode == "ffmpeg" and not self.ffprobe_path:
            self.log("FFprobe missing: metadata probe skipped, running in FFmpeg-only fallback mode.")
        if engine_mode == "gstreamer":
            self.log(
                "GStreamer engine selected. Stream discovery and timed diagnostics will run on "
                "the GStreamer backend."
            )
        else:
            self.log("FFmpeg engine selected. Timed diagnostics will run on the FFmpeg backend.")
        self.log(f"Requested RTSP transport mode: {transport_mode.upper()}")

        if self.auto_preview_var.get() and not self.is_preview_running():
            if self.start_preview():
                self.preview_started_by_test = True

        context = RunContext(
            rtsp_url=rtsp_url,
            duration_seconds=duration_seconds,
            engine_mode=engine_mode,
            transport_mode=transport_mode,
            ffmpeg_path=self.ffmpeg_path,
            ffprobe_path=self.ffprobe_path,
            gst_launch_path=self.gst_launch_path,
            gst_play_path=self.gst_play_path,
            gst_discoverer_path=self.gst_discoverer_path,
            output_dir=str(report_dir),
            run_id=run_id,
        )
        self.worker = DiagnosticWorker(context=context, event_queue=self.event_queue)
        self.worker.start()

        self.test_running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.log(
            f"Started diagnostic for {duration_seconds} seconds on {shorten_text(rtsp_url)} "
            f"using {engine_mode.upper()} selection."
        )

    def stop_test(self) -> None:
        if self.worker and self.test_running:
            self.status_var.set("Stopping...")
            self.worker.request_stop()
            self.log("Stop requested by user.")

    def _drain_events(self) -> None:
        self._sync_preview_state()
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(200, self._drain_events)

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "status":
            self.status_var.set(event.get("message", ""))
            self.log(event.get("message", ""))
            return

        if event_type == "command":
            self.log(shorten_text(event.get("command", ""), max_len=180))
            return

        if event_type == "stream_info":
            info = event.get("info", {})
            selected_transport = str(info.get("selected_transport", "n/a")).upper()
            requested_transport = str(info.get("requested_transport", "n/a")).upper()
            selected_delivery = str(
                info.get("selected_delivery", transport_delivery_label(selected_transport))
            ).upper()
            requested_delivery = str(
                info.get("requested_delivery", transport_delivery_label(requested_transport))
            ).upper()
            stream_count = int(info.get("stream_count", 0) or 0)
            video_count = int(info.get("video_stream_count", 0) or 0)
            audio_count = int(info.get("audio_stream_count", 0) or 0)
            stream_text = (
                f"{info.get('codec_name', 'unknown')} | "
                f"{info.get('width', 0)}x{info.get('height', 0)} | "
                f"{info.get('fps', 0)} FPS"
            )
            self.stream_var.set(stream_text)
            self.transport_live_var.set(
                f"{selected_transport}/{selected_delivery} (Req: {requested_transport}/{requested_delivery})"
            )
            self.stream_counts_var.set(f"{stream_count}/{video_count}/{audio_count}")
            self.log(
                "Stream metadata: "
                f"{stream_text} | transport={selected_transport} ({selected_delivery}) | "
                f"streams={stream_count} (video={video_count}, audio={audio_count})"
            )
            if audio_count <= 0:
                self.log("No audio stream detected. This is supported; diagnostics run on video stream only.")
            for item in info.get("streams", []):
                idx = item.get("index", 0)
                ctype = item.get("codec_type", "unknown")
                codec = item.get("codec_name", "unknown")
                if ctype == "video":
                    extra = (
                        f"{item.get('width', 0)}x{item.get('height', 0)} @ "
                        f"{item.get('fps', 0)} fps"
                    )
                elif ctype == "audio":
                    extra = (
                        f"{item.get('sample_rate_hz', 0)} hz, ch={item.get('channels', 0)}"
                    )
                else:
                    extra = "metadata stream"
                self.log(f"  Stream #{idx} [{ctype}] {codec} | {extra}")
            return

        if event_type == "progress":
            data = event.get("data", {})
            elapsed = float(data.get("elapsed_sec", 0.0) or 0.0)
            frame = int(data.get("frame", 0) or 0)
            expected = float(data.get("expected_frames", 0.0) or 0.0)
            estimated = int(data.get("estimated_drop_frames", 0) or 0)
            ffmpeg_drop = int(data.get("ffmpeg_drop_frames", 0) or 0)
            ffmpeg_dup = int(data.get("dup_frames", 0) or 0)
            bandwidth_now = float(data.get("bandwidth_kbps_current", 0.0) or 0.0)
            bandwidth_avg = float(data.get("bandwidth_kbps_avg", 0.0) or 0.0)
            jitter_percent = float(data.get("instant_fps_jitter_percent", 0.0) or 0.0)
            startup_latency = float(data.get("startup_latency_sec", 0.0) or 0.0)
            missed_packets = int(data.get("rtp_missed_packets", 0) or 0)
            health_score = int(data.get("health_score", 100) or 100)
            health_grade = str(data.get("health_grade", "N/A"))

            self.elapsed_var.set(f"{elapsed:.2f} s")
            self.received_var.set(str(frame))
            self.expected_var.set(str(int(round(expected))))
            self.estimated_drops_var.set(str(estimated))
            self.ffmpeg_drops_var.set(str(ffmpeg_drop))
            self.ffmpeg_dups_var.set(str(ffmpeg_dup))
            self.bandwidth_now_var.set(f"{bandwidth_now:.1f} kbps")
            self.bandwidth_avg_var.set(f"{bandwidth_avg:.1f} kbps")
            self.jitter_var.set(f"{jitter_percent:.2f} %")
            self.startup_latency_var.set(f"{startup_latency:.2f} s")
            self.packet_missed_var.set(str(missed_packets))
            self.health_var.set(f"{health_score} ({health_grade})")
            self.bandwidth_chart_points.append(bandwidth_now)
            self.received_chart_points.append(float(frame))
            self.dropped_chart_points.append(float(estimated))
            self._refresh_live_charts()

            try:
                duration = int(self.duration_var.get())
            except ValueError:
                duration = 0
            if duration > 0:
                percentage = min(100.0, (elapsed / duration) * 100.0)
                self.progress["value"] = percentage
            return

        if event_type == "log":
            line = event.get("line", "")
            self.log(line)
            if should_count_warning(line):
                try:
                    warning_count = int(self.warning_count_var.get()) + 1
                except ValueError:
                    warning_count = 1
                self.warning_count_var.set(str(warning_count))
            return

        if event_type == "completed":
            report = event.get("report", {})
            self.finalize_run(report)
            return

    def finalize_run(self, report_data: dict) -> None:
        run_stamp = str(report_data.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
        report_dir = Path(self.report_dir_var.get())
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Report save error", f"Cannot create report folder:\n{report_dir}\n\n{exc}")
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self.test_running = False
            self.worker = None
            if self.preview_started_by_test and self.is_preview_running():
                self.stop_preview()
            return

        json_path = report_dir / f"camera_diagnostic_{run_stamp}.json"
        pdf_path = report_dir / f"camera_diagnostic_{run_stamp}.pdf"

        try:
            json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Report save error", f"Failed to write JSON report:\n{json_path}\n\n{exc}")
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self.test_running = False
            self.worker = None
            if self.preview_started_by_test and self.is_preview_running():
                self.stop_preview()
            return

        pdf_error = None
        try:
            write_pdf_report(pdf_path, report_data)
        except Exception as exc:
            pdf_error = str(exc)
            self.log(f"PDF generation failed: {pdf_error}")

        self.last_report_var.set(pdf_path.name if not pdf_error else json_path.name)
        self.status_var.set(report_data.get("status", "completed"))
        self.progress["value"] = 100 if report_data.get("status") == "completed" else self.progress["value"]
        deep = report_data.get("deep_diagnostics", {})
        deep_health = deep.get("health", {})
        deep_bandwidth = deep.get("bandwidth_kbps", {})
        transport = report_data.get("transport", {})
        summary = report_data.get("summary", {})
        self.bandwidth_avg_var.set(f"{float(deep_bandwidth.get('avg', 0.0) or 0.0):.1f} kbps")
        self.health_var.set(f"{deep_health.get('score', 'N/A')} ({deep_health.get('grade', 'N/A')})")
        selected_transport = str(transport.get("selected", "N/A")).upper()
        requested_transport = str(transport.get("requested", "N/A")).upper()
        selected_delivery = str(
            transport.get("selected_delivery", transport_delivery_label(str(transport.get("selected", "N/A"))))
        ).upper()
        requested_delivery = str(
            transport.get("requested_delivery", transport_delivery_label(str(transport.get("requested", "N/A"))))
        ).upper()
        self.transport_live_var.set(
            f"{selected_transport}/{selected_delivery} (Req: {requested_transport}/{requested_delivery})"
        )
        self.stream_counts_var.set(
            f"{int(summary.get('stream_count', 0) or 0)}/"
            f"{int(summary.get('video_stream_count', 0) or 0)}/"
            f"{int(summary.get('audio_stream_count', 0) or 0)}"
        )

        self.log(f"JSON report saved: {json_path}")
        if pdf_error:
            self.log("PDF report was not created. Install requirements and run again.")
        else:
            self.log(f"PDF report saved: {pdf_path}")
        if report_data.get("error"):
            self.log(f"Run error: {report_data['error']}")
        snap = report_data.get("snapshot", {})
        if snap.get("path"):
            self.log(f"Snapshot included: {snap.get('path')}")
        elif snap.get("error"):
            self.log(f"Snapshot unavailable: {snap.get('error')}")

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.test_running = False
        self.worker = None
        if self.preview_started_by_test and self.is_preview_running():
            self.stop_preview()

        if pdf_error:
            messagebox.showwarning(
                "Diagnostic Completed with PDF Error",
                f"JSON report created:\n{json_path}\n\nPDF generation failed:\n{pdf_error}",
            )
        else:
            messagebox.showinfo(
                "Diagnostic Completed",
                f"JSON report:\n{json_path}\n\nPDF report:\n{pdf_path}",
            )

    def on_close(self) -> None:
        if self.worker and self.test_running:
            self.worker.request_stop()
            time.sleep(0.2)
        if self.is_preview_running():
            self.stop_preview()
        try:
            self.main_canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = DiagnosticApp(root)
    if not app.ffmpeg_path:
        messagebox.showwarning(
            "FFmpeg Detection",
            "FFmpeg was not auto-detected.\n"
            "Put binaries under C:\\ffmpeg\\bin and restart this app.",
        )
    elif not app.ffprobe_path:
        messagebox.showwarning(
            "FFprobe Detection",
            "FFprobe was not detected.\n"
            "App will run in FFmpeg fallback mode (less metadata precision).",
        )
    if not app.ffplay_path:
        if not app._has_gstreamer_preview_backend():
            messagebox.showwarning(
                "Preview Backend Detection",
                "Neither FFplay nor GStreamer preview binaries were detected.\n"
                "Diagnostics still work, but live preview will be unavailable.",
            )
    root.mainloop()


if __name__ == "__main__":
    main()
