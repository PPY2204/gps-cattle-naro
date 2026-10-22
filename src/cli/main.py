"""Command Line Interface (CLI) entrypoint for """
import argparse
import os
from pathlib import Path
import re
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from acquisition.reconnect import ResilientStreamReader
from config import env_bool, env_float, env_int, load_dotenv, set_dotenv_value
from device.session import SessionManager
from domain.enums import MountType, SubjectType
from domain.models import GPSMeasurement, SubjectProfile
from services.export import export_clean_csv, export_geojson
from services.recorder import RecorderService
from services.replay import ReplayService


def get_profile(name: str) -> SubjectProfile:
    name_clean = name.strip().lower()
    if name_clean in ("cow", "bovine", "cattle"):
        return SubjectProfile.cow_default()
    elif name_clean in ("human", "person", "walker"):
        return SubjectProfile.human_default()
    elif name_clean in ("other", "wildlife", "ovine", "vehicle"):
        return SubjectProfile.other_default()
    else:
        return SubjectProfile(
            subject_type=SubjectType(name.upper()),
        )


def _session_recorded_at(filename: str) -> str | None:
    """Return the fixed Japan-time start encoded in a session filename."""
    match = re.search(r"SESSION_(\d{8})_(\d{6})_\d{6}", filename)
    if not match:
        return None
    try:
        started = datetime.strptime(
            f"{match.group(1)}_{match.group(2)}", "%Y%m%d_%H%M%S"
        ).replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    except ValueError:
        return None
    return started.strftime("%Y-%m-%d %H:%M:%S JST")


import http.server
import json
import socketserver
import threading


class LiveStatusHandler(http.server.BaseHTTPRequestHandler):
    """Zero-dependency HTTP server serving live status and diagnostic dashboard."""
    recorder_ref: Optional[RecorderService] = None

    def log_message(self, format, *args):
        pass  # Suppress console clutter

    def do_GET(self):
        if self.path == "/api/gps" or self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            
            rec = LiveStatusHandler.recorder_ref
            is_rec = rec.is_recording if rec else False
            meas = rec.latest_measurement if rec else None
            summary = rec.health.summary() if rec else {}
            
            data = {
                "online": True,
                "recording": is_rec,
                "recording_state": rec.recording_state if rec else "STOPPED",
                "session_id": rec.session_mgr.session_id if rec else None,
                "record_started_at": (
                    rec.session_mgr.metadata.start_time.astimezone(ZoneInfo("Asia/Tokyo")).isoformat()
                    if rec else None
                ),
                "subject_id": rec.session_mgr.metadata.subject_id if rec else "COW-NARO-001",
                "profile": {
                    SubjectType.BOVINE: "cow",
                    SubjectType.HUMAN: "human",
                    SubjectType.OTHER: "other",
                }.get(rec.profile.subject_type if rec else SubjectType.HUMAN, "human"),
                "uptime_seconds": summary.get("uptime_seconds", 0),
                "sampling_hz": summary.get("current_sampling_hz", 0.0),
                "total_sentences": summary.get("total_sentences", 0),
                "accepted_epochs": summary.get("accepted_epochs", 0),
                "rejected_epochs": summary.get("rejected_epochs", 0),
                "last_sentence": rec.health.last_sentence if rec else "",
                "latitude": meas.latitude if meas else None,
                "longitude": meas.longitude if meas else None,
                "altitude": meas.altitude if meas else None,
                "speed_kmh": meas.speed_kmh if meas else None,
                "satellites": meas.satellites if meas else 0,
                "hdop": meas.hdop if meas else None,
                "fix_quality": meas.fix_quality.value if meas else "INVALID",
                "behavior": meas.behavior_state.value if meas else "UNKNOWN",
                "accepted": meas.is_accepted if meas else False,
            }
            self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))
            return

        elif self.path.startswith("/api/profile"):
            from urllib.parse import parse_qs, urlparse

            rec = LiveStatusHandler.recorder_ref
            if rec is None:
                self.send_response(503)
                self.end_headers()
                return
            selected = parse_qs(urlparse(self.path).query).get("type", [""])[0].lower()
            if selected not in {"human", "cow", "other"}:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": "type must be human, cow, or other"}).encode("utf-8"))
                return
            profile = get_profile(selected)
            try:
                session_id = rec.switch_profile(profile)
                set_dotenv_value("GPS_PROFILE", selected)
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "profile_updated",
                "profile": selected,
                "recording": rec.is_recording,
                "session_id": session_id,
            }).encode("utf-8"))
            return

        elif self.path == "/api/record/start":
            rec = LiveStatusHandler.recorder_ref
            if rec is None:
                self.send_response(503)
                self.end_headers()
                return
            try:
                session_id = rec.start_new_session()
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "recording_started", "session_id": session_id}).encode("utf-8"))
            return

        elif self.path == "/api/record/stop":
            rec = LiveStatusHandler.recorder_ref
            if rec is not None:
                try:
                    rec.stop_recording()
                except Exception as exc:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))
                    return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "recording_stopped"}).encode("utf-8"))
            return

        elif self.path.startswith("/api/file/delete?file="):
            file_param = self.path.split("/api/file/delete?file=", 1)[1]
            import urllib.parse
            clean_path = Path(urllib.parse.unquote(file_param))
            if clean_path.exists() and clean_path.is_file() and "data" in clean_path.parts:
                try:
                    clean_path.unlink()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "deleted"}).encode("utf-8"))
                    return
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    return
            self.send_response(404)
            self.end_headers()
            return

        elif self.path == "/api/files":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            files_list = []
            storage_root = Path(os.environ.get(
                "GPS_STORAGE_ROOT",
                str(Path.home() / "naro-gps-nathan" / "data"),
            ))
            scan_dirs = [
                storage_root / "processed",
                storage_root / "raw",
                storage_root / "exports",
            ]
            seen_names = set()
            for root_dir in scan_dirs:
                if root_dir.exists():
                    for p in sorted(root_dir.rglob("*"), reverse=True):
                        if p.is_file() and p.suffix in [".csv", ".nmea", ".db", ".geojson"] and p.name not in seen_names:
                            seen_names.add(p.name)
                            ftype = "processed" if p.suffix in [".csv", ".geojson"] else ("raw" if p.suffix == ".nmea" else "db")
                            files_list.append({
                                "name": p.name,
                                "type": ftype,
                                "ext": p.suffix.lstrip("."),
                                "path": str(p.as_posix()),
                                "size_kb": round(p.stat().st_size / 1024, 1),
                                "recorded_at": _session_recorded_at(p.name) or time.strftime(
                                    "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)
                                ),
                            })
            self.wfile.write(json.dumps(files_list, indent=2).encode("utf-8"))
            return

        elif self.path.startswith("/download?file="):
            file_param = self.path.split("/download?file=", 1)[1]
            import urllib.parse
            clean_path = Path(urllib.parse.unquote(file_param))
            if clean_path.exists() and clean_path.is_file() and "data" in clean_path.parts:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{clean_path.name}"')
                self.send_header("Content-Length", str(clean_path.stat().st_size))
                self.end_headers()
                with open(clean_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # Interactive Field Dashboard (with Offline Radar, Online OSM, and File Filters)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>NARO GNSS Field Monitor</title>
  <!-- Optional Leaflet (loads if internet available, fallback to Canvas radar if offline) -->
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" async></script>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 12px; }
    .card { background: #1e293b; border-radius: 12px; padding: 14px; margin-bottom: 12px; border: 1px solid #334155; }
    .title { font-size: 1.05rem; font-weight: bold; margin-bottom: 8px; color: #38bdf8; display: flex; align-items: center; justify-content: space-between; }
    .row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #334155; font-size: 0.9rem; }
    .val { font-family: monospace; font-weight: bold; color: #e2e8f0; }
    .badge { padding: 4px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; }
    .badge-ok { background: #166534; color: #86efac; }
    .badge-warn { background: #854d0e; color: #fef08a; }
    .badge-off { background: #991b1b; color: #fca5a5; }
    .badge-standby { background: #475569; color: #cbd5e1; }
    
    /* Map & Radar styling */
    .map-container { position: relative; width: 100%; border-radius: 8px; overflow: hidden; background: #090d16; border: 1px solid #475569; }
    #radar-canvas { width: 100%; height: 280px; display: block; }
    #leaflet-map { width: 100%; height: 280px; display: none; }
    .map-controls { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 0.8rem; }
    .tab-btn { background: #334155; color: #cbd5e1; border: none; padding: 5px 10px; border-radius: 6px; cursor: pointer; font-weight: 500; font-size: 0.75rem; }
    .tab-btn.active { background: #0284c7; color: white; }
    
    /* File list styling */
    .filter-bar { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 6px; margin-bottom: 8px; }
    .filter-btn { background: #334155; color: #94a3b8; border: none; padding: 4px 10px; border-radius: 20px; font-size: 0.75rem; cursor: pointer; white-space: nowrap; }
    .filter-btn.active { background: #38bdf8; color: #0f172a; font-weight: bold; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { text-align: left; padding: 7px 6px; background: #0f172a; color: #94a3b8; border-bottom: 1px solid #475569; }
    td { padding: 8px 6px; border-bottom: 1px solid #334155; vertical-align: middle; }
    .tag { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; margin-right: 4px; }
    .tag-csv { background: #166534; color: #bbf7d0; }
    .tag-raw { background: #1e3a8a; color: #bfdbfe; }
    .tag-db { background: #581c87; color: #e9d5ff; }
    .btn { background: #0284c7; color: white; padding: 5px 9px; border-radius: 4px; text-decoration: none; font-size: 0.75rem; border: none; cursor: pointer; display: inline-block; font-weight: 500; }
    .btn-del { background: #dc2626; margin-left: 4px; }
    .raw-box { font-family: monospace; font-size: 0.75rem; background: #090d16; color: #38bdf8; padding: 8px; border-radius: 6px; word-break: break-all; margin-top: 6px; border: 1px solid #334155; }
  </style>
</head>
<body>
  <!-- Header & Main Trigger -->
  <div class="card">
    <div class="title">
      <span>🛰️ NARO GNSS Field Monitor</span>
      <span id="badge" class="badge badge-standby">STANDBY</span>
    </div>
    <div class="row"><span>Subject:</span><span id="subject" class="val">--</span></div>
    <div class="row"><span>Collect as:</span>
      <select id="profile-select" onchange="changeProfile(this.value)" style="background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:4px;padding:4px;">
        <option value="human">Human</option>
        <option value="cow">Cow</option>
        <option value="other">Other</option>
      </select>
    </div>
    <div class="row"><span>Session:</span><span id="session" class="val">--</span></div>
    <div class="row"><span>Record started:</span><span id="record-started" class="val">--</span></div>
    <div class="row"><span>Rate:</span><span id="hz" class="val">0.0 Hz</span></div>
    <div style="margin-top: 12px;">
      <button id="btn-toggle-rec" onclick="toggleRecording()" style="width: 100%; padding: 12px; border-radius: 8px; font-weight: bold; cursor: pointer; border: none; background: #16a34a; color: white; font-size: 1rem; box-shadow: 0 2px 8px rgba(0,0,0,0.3);">
        ▶ Start Recording
      </button>
    </div>
  </div>

  <!-- Realtime Map / Field Radar -->
  <div class="card">
    <div class="title">
      <span>🗺️ Live Trajectory & Field Radar</span>
      <div style="display: flex; gap: 4px;">
        <button id="btn-tab-radar" class="tab-btn active" onclick="switchMapMode('radar')">🧭 Radar (Offline)</button>
        <button id="btn-tab-osm" class="tab-btn" onclick="switchMapMode('osm')">🌐 Map (OSM)</button>
      </div>
    </div>
    <div class="map-container">
      <canvas id="radar-canvas" width="600" height="280"></canvas>
      <div id="leaflet-map"></div>
    </div>
    <div class="map-controls">
      <span id="radar-info" style="color: #94a3b8; font-family: monospace;">Radar Grid: 10m scale</span>
      <div>
        <button class="tab-btn" onclick="zoomRadar(1.2)">➕</button>
        <button class="tab-btn" onclick="zoomRadar(0.8)">➖</button>
        <button class="tab-btn" onclick="resetRadarTrail()">🧹 Clear Trail</button>
      </div>
    </div>
  </div>

  <!-- Telemetry Metrics -->
  <div class="card">
    <div class="title">📍 Telemetry Metrics</div>
    <div class="row"><span>Satellites:</span><span id="sats" class="val">0</span></div>
    <div class="row"><span>HDOP Noise:</span><span id="hdop" class="val">--</span></div>
    <div class="row"><span>Fix Quality:</span><span id="fix" class="val">--</span></div>
    <div class="row"><span>Coordinates:</span><span id="coords" class="val">--</span></div>
    <div class="row"><span>Speed:</span><span id="speed" class="val">-- km/h</span></div>
    <div class="row"><span>Behavior:</span><span id="behavior" class="val">--</span></div>
  </div>

  <!-- Raw NMEA Sentence -->
  <div class="card">
    <div class="title">⚡ Raw NMEA Sentence</div>
    <div id="raw" class="raw-box">Waiting for NMEA stream...</div>
  </div>

  <!-- File Manager with Filter Tabs -->
  <div class="card">
    <div class="title">
      <span>📂 Recorded Data Files</span>
      <button onclick="fetchFiles()" class="tab-btn">🔄 Refresh</button>
    </div>
    <div class="filter-bar">
      <button class="filter-btn active" onclick="setFileFilter('all', this)">All Files (<span id="cnt-all">0</span>)</button>
      <button class="filter-btn" onclick="setFileFilter('processed', this)">📊 Processed CSV (<span id="cnt-csv">0</span>)</button>
      <button class="filter-btn" onclick="setFileFilter('raw', this)">⚡ Raw NMEA (<span id="cnt-raw">0</span>)</button>
      <button class="filter-btn" onclick="setFileFilter('db', this)">🗄️ Database DB (<span id="cnt-db">0</span>)</button>
    </div>
    <div style="overflow-x: auto; max-height: 320px; overflow-y: auto;">
      <table>
        <thead>
          <tr>
            <th>File Info</th>
            <th>Size</th>
            <th style="width: 120px; text-align: right;">Action</th>
          </tr>
        </thead>
        <tbody id="files-tbody">
          <tr><td colspan="3" style="text-align: center; color: #94a3b8; padding: 14px;">Loading files...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    let isRec = false;
    let trackHistory = []; // [{lat, lon, time}]
    let radarScale = 0.0001; // ~10m in lat/lon degrees
    let mapMode = 'radar';
    let leafletMap = null;
    let leafletMarker = null;
    let leafletPolyline = null;
    let allFiles = [];
    let currentFilter = 'all';

    // Canvas Radar Renderer (Works 100% Offline with Zero External Libs)
    const canvas = document.getElementById('radar-canvas');
    const ctx = canvas.getContext('2d');

    function resizeCanvas() {
      canvas.width = canvas.parentElement.clientWidth * window.devicePixelRatio;
      canvas.height = 280 * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    }
    window.addEventListener('resize', resizeCanvas);
    resizeCanvas();

    function drawRadar(currentLat, currentLon, speedKmh, heading) {
      const w = canvas.width / window.devicePixelRatio;
      const h = 280;
      const cx = w / 2;
      const cy = h / 2;

      ctx.clearRect(0, 0, w, h);

      // Radar background
      ctx.fillStyle = '#090d16';
      ctx.fillRect(0, 0, w, h);

      // Concentric Radar Rings
      const maxR = Math.min(cx, cy) - 20;
      ctx.strokeStyle = '#1e293b';
      ctx.lineWidth = 1;
      for (let r = maxR / 3; r <= maxR; r += maxR / 3) {
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Crosshairs
      ctx.beginPath();
      ctx.moveTo(cx, 10); ctx.lineTo(cx, h - 10);
      ctx.moveTo(10, cy); ctx.lineTo(w - 10, cy);
      ctx.stroke();

      // Cardinal labels
      ctx.fillStyle = '#64748b';
      ctx.font = '10px monospace';
      ctx.fillText('N', cx - 3, 14);
      ctx.fillText('S', cx - 3, h - 4);
      ctx.fillText('E', w - 14, cy + 3);
      ctx.fillText('W', 4, cy + 3);

      if (currentLat == null || currentLon == null) {
        ctx.fillStyle = '#94a3b8';
        ctx.font = '12px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for GPS lock...', cx, cy);
        ctx.textAlign = 'left';
        return;
      }

      // Draw Trajectory Trail
      if (trackHistory.length > 1) {
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        for (let i = 0; i < trackHistory.length; i++) {
          const pt = trackHistory[i];
          const dx = (pt.lon - currentLon) / radarScale * (maxR / 2);
          const dy = -(pt.lat - currentLat) / radarScale * (maxR / 2);
          const px = cx + dx;
          const py = cy + dy;
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        ctx.stroke();

        // Dots on trail
        ctx.fillStyle = '#0284c7';
        for (let i = 0; i < trackHistory.length; i += Math.max(1, Math.floor(trackHistory.length / 30))) {
          const pt = trackHistory[i];
          const dx = (pt.lon - currentLon) / radarScale * (maxR / 2);
          const dy = -(pt.lat - currentLat) / radarScale * (maxR / 2);
          ctx.beginPath();
          ctx.arc(cx + dx, cy + dy, 2, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // Current Position Blip (Center)
      ctx.fillStyle = isRec ? '#ef4444' : '#22c55e';
      ctx.beginPath();
      ctx.arc(cx, cy, 6, 0, Math.PI * 2);
      ctx.fill();

      // Pulsing Ring
      ctx.strokeStyle = isRec ? 'rgba(239, 68, 68, 0.4)' : 'rgba(34, 197, 94, 0.4)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, 12, 0, Math.PI * 2);
      ctx.stroke();

      // Radar HUD
      const approxMeters = Math.round(radarScale * 111320);
      document.getElementById('radar-info').innerText = `Trail: ${trackHistory.length} pts | Radius: ~${approxMeters}m`;
    }

    function zoomRadar(factor) {
      radarScale = Math.max(0.00001, Math.min(0.01, radarScale * factor));
    }

    function resetRadarTrail() {
      trackHistory = [];
      if (leafletPolyline) leafletPolyline.setLatLngs([]);
    }

    // Switch between Offline Radar and OpenStreetMap
    function switchMapMode(mode) {
      mapMode = mode;
      document.getElementById('btn-tab-radar').className = mode === 'radar' ? 'tab-btn active' : 'tab-btn';
      document.getElementById('btn-tab-osm').className = mode === 'osm' ? 'tab-btn active' : 'tab-btn';
      
      const radarEl = document.getElementById('radar-canvas');
      const leafletEl = document.getElementById('leaflet-map');

      if (mode === 'radar') {
        radarEl.style.display = 'block';
        leafletEl.style.display = 'none';
      } else {
        radarEl.style.display = 'none';
        leafletEl.style.display = 'block';
        initLeafletIfNeeded();
      }
    }

    function initLeafletIfNeeded() {
      if (typeof L === 'undefined') {
        alert('Leaflet requires active internet connection. Please use the offline Radar mode.');
        switchMapMode('radar');
        return;
      }
      if (!leafletMap) {
        leafletMap = L.map('leaflet-map').setView([36.03, 140.13], 17);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
          maxZoom: 19,
          attribution: '© OpenStreetMap'
        }).addTo(leafletMap);
        leafletPolyline = L.polyline([], {color: '#38bdf8', weight: 4}).addTo(leafletMap);
      }
      setTimeout(() => leafletMap.invalidateSize(), 200);
    }

    // Update GPS Telemetry
    async function update() {
      try {
        const res = await fetch('/api/gps');
        const d = await res.json();
        const badge = document.getElementById('badge');

        isRec = !!d.recording;
        updateRecButton(isRec);

        document.getElementById('subject').innerText = d.subject_id || '--';
        document.getElementById('profile-select').value = d.profile || 'human';
        document.getElementById('session').innerText = d.session_id || '--';
        document.getElementById('record-started').innerText = d.record_started_at
          ? new Date(d.record_started_at).toLocaleString()
          : '--';
        document.getElementById('hz').innerText = (d.sampling_hz || 0).toFixed(1) + ' Hz';
        document.getElementById('sats').innerText = d.satellites;
        document.getElementById('hdop').innerText = d.hdop != null ? d.hdop.toFixed(2) : '--';
        document.getElementById('fix').innerText = d.fix_quality;
        
        if (d.latitude != null && d.longitude != null) {
          document.getElementById('coords').innerText = d.latitude.toFixed(6) + ', ' + d.longitude.toFixed(6);
          // Add to track history
          trackHistory.push({ lat: d.latitude, lon: d.longitude, time: Date.now() });
          if (trackHistory.length > 500) trackHistory.shift();

          // Leaflet update
          if (leafletMap && typeof L !== 'undefined') {
            const pos = [d.latitude, d.longitude];
            if (!leafletMarker) leafletMarker = L.marker(pos).addTo(leafletMap);
            else leafletMarker.setLatLng(pos);
            leafletPolyline.addLatLng(pos);
            leafletMap.panTo(pos);
          }
        } else {
          document.getElementById('coords').innerText = '--';
        }

        document.getElementById('speed').innerText = d.speed_kmh != null ? d.speed_kmh.toFixed(1) + ' km/h' : '--';
        document.getElementById('behavior').innerText = d.behavior;
        document.getElementById('raw').innerText = d.last_sentence || 'Listening...';

        // Badge Status
        if (!isRec) {
          badge.className = 'badge badge-standby';
          badge.innerText = d.satellites >= 4 ? `STANDBY (${d.satellites} sats)` : 'STANDBY (NO FIX)';
        } else {
          badge.className = 'badge badge-ok';
          badge.innerText = `🔴 RECORDING (${d.satellites} sats)`;
        }

        // Redraw canvas
        drawRadar(d.latitude, d.longitude, d.speed_kmh, 0);

      } catch (e) {
        document.getElementById('badge').className = 'badge badge-off';
        document.getElementById('badge').innerText = 'OFFLINE';
      }
    }

    // Start / Stop Recording
    async function toggleRecording() {
      const endpoint = isRec ? '/api/record/stop' : '/api/record/start';
      const res = await fetch(endpoint);
      if (!res.ok) return;
      await update();
      if (isRec) {
        resetRadarTrail();
      }
      setTimeout(fetchFiles, 600);
    }

    async function changeProfile(profile) {
      try {
        const res = await fetch('/api/profile?type=' + encodeURIComponent(profile));
        if (!res.ok) {
          alert('Could not change collection profile');
          await update();
          return;
        }
        await update();
      } catch (e) {
        alert('Could not change collection profile');
        await update();
      }
    }

    function updateRecButton(rec) {
      isRec = rec;
      const btn = document.getElementById('btn-toggle-rec');
      if (btn) {
        if (rec) {
          btn.style.background = '#dc2626';
          btn.innerText = '⏹ Stop Recording';
        } else {
          btn.style.background = '#16a34a';
          btn.innerText = '▶ Start Recording';
        }
      }
    }

    // File Management & Filters
    async function fetchFiles() {
      try {
        const res = await fetch('/api/files');
        allFiles = await res.json();
        
        // Count files per category
        const csvCount = allFiles.filter(f => f.type === 'processed').length;
        const rawCount = allFiles.filter(f => f.type === 'raw').length;
        const dbCount = allFiles.filter(f => f.type === 'db').length;

        document.getElementById('cnt-all').innerText = allFiles.length;
        document.getElementById('cnt-csv').innerText = csvCount;
        document.getElementById('cnt-raw').innerText = rawCount;
        document.getElementById('cnt-db').innerText = dbCount;

        renderFilteredFiles();
      } catch (e) {}
    }

    function setFileFilter(filter, el) {
      currentFilter = filter;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      if (el) el.classList.add('active');
      renderFilteredFiles();
    }

    function renderFilteredFiles() {
      const tbody = document.getElementById('files-tbody');
      const filtered = currentFilter === 'all' ? allFiles : allFiles.filter(f => f.type === currentFilter);

      if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: #94a3b8; padding: 16px;">No matching files found.</td></tr>';
        return;
      }

      tbody.innerHTML = filtered.map(f => {
        let tagClass = 'tag-raw';
        let tagLabel = 'RAW NMEA';
        if (f.type === 'processed') {
          tagClass = 'tag-csv';
          tagLabel = 'CLEAN CSV';
        } else if (f.type === 'db') {
          tagClass = 'tag-db';
          tagLabel = 'SQLITE DB';
        }

        return `
          <tr>
            <td>
              <div><span class="tag ${tagClass}">${tagLabel}</span><strong>${f.name}</strong></div>
              <small style="color: #94a3b8;">Recorded: ${f.recorded_at}</small>
            </td>
            <td style="font-family: monospace; white-space: nowrap;">${f.size_kb} KB</td>
            <td style="text-align: right; white-space: nowrap;">
              <a class="btn" href="/download?file=${encodeURIComponent(f.path)}">⬇️ Get</a>
              <button class="btn btn-del" onclick="deleteFile('${encodeURIComponent(f.path)}')">🗑️</button>
            </td>
          </tr>
        `;
      }).join('');
    }

    async function deleteFile(encodedPath) {
      if (!confirm('Are you sure you want to permanently delete this file?')) return;
      try {
        await fetch('/api/file/delete?file=' + encodedPath);
        fetchFiles();
      } catch (e) {
        alert('Failed to delete file');
      }
    }

    setInterval(update, 1000);
    update();
    fetchFiles();
  </script>
</body>
</html>"""
        self.wfile.write(html.encode("utf-8"))


def start_monitor_server(port: int = 8000) -> Optional[socketserver.TCPServer]:
    try:
        class ReuseTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        server = ReuseTCPServer(("0.0.0.0", port), LiveStatusHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server
    except Exception as e:
        print(f"Warning: Could not bind web monitor on port {port}: {e}")
        return None


def cmd_record(args: argparse.Namespace) -> int:
    """Run live GPS acquisition directly to SD card with optional instant web monitor."""
    profile = get_profile(args.profile)
    mount = MountType(args.mount.upper()) if hasattr(MountType, args.mount.upper()) else MountType.COLLAR

    print(f"=== Starting GPS Acquisition & Telemetry ===")
    print(f"Port:       {args.port}")
    print(f"Baud rate:  {args.baud}")
    print(f"Profile:    {profile.subject_type.value} (Max speed: {profile.max_speed_kmh} km/h)")
    print(f"Subject ID: {args.subject_id}")
    print(f"Mount Type: {mount.value}")
    print("Mode:       RECORDING (automatic recording enabled)")

    session_mgr = SessionManager(
        device_id=args.device_id,
        subject_id=args.subject_id,
        subject_type=profile.subject_type,
        mount_type=mount,
        sampling_rate_hz=args.hz,
    )

    # Always anchor data directories to ~/naro-gps-nathan/data or project root, never relative to current cwd!
    base_data = Path(os.environ.get(
        "GPS_STORAGE_ROOT",
        str(Path.home() / "naro-gps-nathan" / "data"),
    ))
    raw_path = Path(args.raw_dir) if Path(args.raw_dir).is_absolute() else base_data / "raw"
    processed_path = Path(args.processed_dir) if Path(args.processed_dir).is_absolute() else base_data / "processed"

    recorder = RecorderService(
        session_mgr=session_mgr,
        profile=profile,
        raw_dir=raw_path,
        processed_dir=processed_path,
        min_satellites=args.min_sats,
        max_hdop=args.max_hdop,
        enable_sqlite=getattr(args, "enable_db", False),
        is_recording=True,
    )

    web_server = None
    if getattr(args, "web", False):
        LiveStatusHandler.recorder_ref = recorder
        web_server = start_monitor_server(port=args.web_port)
        print(f"Web Monitor: http://0.0.0.0:{args.web_port} (Open in phone browser to inspect without SSH)")

    print("Press Ctrl+C to stop recording.")

    def on_reconnected():
        session_id = recorder.rotate_session()
        print(f"Reconnected to GNSS; started new session {session_id}")

    reader = ResilientStreamReader(
        port=args.port,
        baud_rate=args.baud,
        timeout=args.timeout,
        reconnect_delay_seconds=args.reconnect_delay,
        rate_hz=args.gps_rate_hz,
        configure_rate=args.configure_rate,
        bluetooth_mac=args.bluetooth_mac,
        rfcomm_index=args.rfcomm_index,
        rfcomm_channel=args.rfcomm_channel,
        on_connection_callback=on_reconnected,
    )

    # Print realtime line on terminal so user sees data streaming immediately
    last_print_time = [0.0]

    def on_measurement_tick(m: GPSMeasurement):
        t_now = time.time()
        if t_now - last_print_time[0] >= 1.0:  # print once per second
            last_print_time[0] = t_now
            sats = m.satellites
            hdop = f"{m.hdop:.1f}" if m.hdop is not None else "--"
            speed = f"{m.speed_kmh:.1f}" if m.speed_kmh is not None else "--"
            fix = m.fix_quality.value
            status_str = "FIX OK" if m.is_accepted else "REJECTED"
            rec_tag = "REC" if recorder.is_recording else "STANDBY"
            print(f"[{m.utc_time}] [{rec_tag}] Sats:{sats:2d} | HDOP:{hdop:>4s} | Fix:{fix} | Speed:{speed:>4s}km/h | {status_str} | {m.behavior_state.value}")

    recorder.on_measurement = on_measurement_tick

    def request_shutdown(signum, frame):
        print(f"\nReceived signal {signum}; stopping acquisition...")
        reader.stop()

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        recorder.run_stream(reader.stream_lines())
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if web_server:
            web_server.shutdown()
        reader.stop()
        recorder.stop_recording()
        summary = recorder.health.summary()
        print("\n=== Session Summary ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Run replay test from recorded fixture file."""
    profile = get_profile(args.profile)
    filepath = Path(args.file)

    print(f"=== Replaying GPS Stream ===")
    print(f"Source file: {filepath}")
    print(f"Profile:     {profile.subject_type.value} (Max speed: {profile.max_speed_kmh} km/h)")
    print(f"Realtime:    {args.realtime} ({args.hz} Hz)")

    replay = ReplayService(
        filepath=filepath,
        profile=profile,
        subject_id=args.subject_id,
        output_dir=Path(args.output_dir),
        realtime_delay=args.realtime,
        simulated_hz=args.hz,
    )

    summary = replay.run()
    print("\n=== Replay Finished Successfully ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start standalone web dashboard for inspecting past logs and downloading files."""
    print(f"=== Starting NARO GPS Web Server ===")
    print(f"Dashboard available at: http://0.0.0.0:{args.port} (or http://{args.host}:{args.port})")
    print("Press Ctrl+C to stop.")
    server = start_monitor_server(port=args.port)
    if not server:
        return 1
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.shutdown()
def cmd_export(args: argparse.Namespace) -> int:
    """Export processed CSV to GeoJSON or clean CSV."""
    in_p = Path(args.input)
    fmt = args.format.lower()

    if fmt == "geojson":
        out_p = Path(args.output) if args.output else in_p.with_suffix(".geojson")
        res = export_geojson(in_p, out_p)
        print(f"Exported {res['total_exported_points']} points to GeoJSON: {out_p}")
    else:
        out_p = Path(args.output) if args.output else in_p.parent / f"{in_p.stem}_clean.csv"
        count = export_clean_csv(in_p, out_p)
        print(f"Exported {count} clean accepted points to: {out_p}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gps",
        description="NARO GPS: Modular IoT GPS Acquisition and Kinematic Tracking for Field Research",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Command: serve
    srv = subparsers.add_parser("serve", help="Start standalone web dashboard to view files and logs")
    srv.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    srv.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    srv.set_defaults(func=cmd_serve)

    # Command: record
    rec = subparsers.add_parser("record", help="Start live recording from GPS serial port to disk")
    rec.add_argument("--port", default=os.environ.get("GPS_PORT", "/dev/rfcomm0"), help="Serial port")
    rec.add_argument("--baud", type=int, default=env_int("GPS_BAUD", 9600), help="Serial baud rate")
    rec.add_argument("--profile", default=os.environ.get("GPS_PROFILE", "human"), choices=["cow", "human", "other"], help="Subject profile")
    rec.add_argument("--subject-id", default=os.environ.get("GPS_SUBJECT_ID", "COW-001"), help="Identifier")
    rec.add_argument("--device-id", default=os.environ.get("GPS_DEVICE_ID", "PI-ZERO-01"), help="Device ID")
    rec.add_argument("--mount", default=os.environ.get("GPS_MOUNT", "COLLAR"), help="Mount type")
    rec.add_argument("--hz", type=int, default=env_int("GPS_RATE_HZ", 4), help="Session sampling rate")
    rec.add_argument("--gps-rate-hz", type=float, default=env_float("GPS_RATE_HZ", 4.0), help="GNSS receiver rate")
    rec.add_argument("--configure-rate", action=argparse.BooleanOptionalAction, default=env_bool("GPS_CONFIGURE_RATE", True), help="Configure receiver rate")
    rec.add_argument("--timeout", type=float, default=env_float("GPS_TIMEOUT_SECONDS", 2.0), help="Serial timeout")
    rec.add_argument("--reconnect-delay", type=float, default=env_float("GPS_RECONNECT_DELAY_SECONDS", 3.0), help="Reconnect delay")
    rec.add_argument("--bluetooth-mac", default=os.environ.get("GPS_BLUETOOTH_MAC", ""), help="Drogger Bluetooth MAC")
    rec.add_argument("--rfcomm-index", type=int, default=env_int("GPS_RFCOMM_INDEX", 0), help="RFCOMM device index")
    rec.add_argument("--rfcomm-channel", type=int, default=env_int("GPS_RFCOMM_CHANNEL", 1), help="Bluetooth RFCOMM channel")
    rec.add_argument("--min-sats", type=int, default=env_int("GPS_MIN_SATELLITES", 4), help="Min satellites")
    rec.add_argument("--max-hdop", type=float, default=env_float("GPS_MAX_HDOP", 5.0), help="Max HDOP")
    rec.add_argument("--raw-dir", default=os.environ.get("GPS_RAW_DIR", "data/raw"), help="Raw NMEA directory")
    rec.add_argument("--processed-dir", default=os.environ.get("GPS_PROCESSED_DIR", "data/processed"), help="Processed directory")
    rec.add_argument(
        "--enable-db",
        action=argparse.BooleanOptionalAction,
        default=env_bool("GPS_ENABLE_SQLITE", False),
        help="Enable optional SQLite storage",
    )
    rec.add_argument("--web", action="store_true", help="Launch lightweight instant HTTP monitor dashboard")
    rec.add_argument("--web-port", type=int, default=env_int("GPS_WEB_PORT", 8000), help="Web monitor port")
    rec.set_defaults(func=cmd_record)

    # Command: replay
    rep = subparsers.add_parser("replay", help="Replay an NMEA fixture through the pipeline")
    rep.add_argument("--file", "-f", required=True, help="Path to raw NMEA text file")
    rep.add_argument("--profile", default="cow", choices=["cow", "human", "other"], help="Subject profile")
    rep.add_argument("--subject-id", default="SIM-001", help="Simulated subject ID")
    rep.add_argument("--output-dir", default="data/processed", help="Output directory for processed CSV")
    rep.add_argument("--realtime", action="store_true", help="Simulate real-time rate delay")
    rep.add_argument("--hz", type=float, default=10.0, help="Simulated Hz rate")
    rep.set_defaults(func=cmd_replay)

    # Command: export
    exp = subparsers.add_parser("export", help="Export processed trajectory")
    exp.add_argument("--input", "-i", required=True, help="Processed session CSV")
    exp.add_argument("--format", choices=["geojson", "clean-csv"], default="geojson", help="Export format")
    exp.add_argument("--output", "-o", default=None, help="Output destination path")
    exp.set_defaults(func=cmd_export)

    return parser


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
