import os
import re
import json
import time
import requests
import cv2
import streamlit as st
import paho.mqtt.client as mqtt
from PIL import Image
from dotenv import load_dotenv
from google import genai

load_dotenv(override=True)

st.set_page_config(page_title="Kobra S1 Print Doctor", page_icon="🖨️", layout="wide")
st.title("🖨️ Anycubic Kobra S1 LAN Print Doctor")
st.caption("Live LAN Telemetry & Camera Diagnostics Powered by **Google Gemma 4** | Hacklab Mikkeli")

DEFAULT_IP = os.getenv("ANYCUBIC_S1_IP", "192.168.8.68")
MQTT_BROKER = os.getenv("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# Initialize Session State Defaults for Live Telemetry
if "parsed_temps" not in st.session_state:
    st.session_state["parsed_temps"] = {
        "nozzle_actual": 215,
        "nozzle_target": 215,
        "bed_actual": 60,
        "bed_target": 60,
        "chamber_temp": 32,
        "state": "idle",
        "progress": 0,
        "ace_drying": False,
        "light_on": True
    }

def get_ai_client():
    os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
    os.environ.pop("GOOGLE_CLOUD_API_KEY", None)

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    studio_key = os.getenv("GEMINI_API_KEY")

    if studio_key:
        return genai.Client(vertexai=False, api_key=studio_key)
    elif project_id:
        return genai.Client(vertexai=True, project=project_id, location=location)
    return None

# 1. Fetch Printer HTTP Discovery Info (Port 18910)
def fetch_lan_discovery(printer_ip):
    try:
        resp = requests.get(f"http://{printer_ip}:18910/info", timeout=2.5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        return {"status": "unreachable", "detail": str(e)}
    return {"status": "no_response"}

# 2. Fetch & Parse Live Temps from MQTT Bridge
def fetch_and_parse_telemetry(broker, port, timeout=1.8):
    raw_topics = {}

    def on_message(client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
            if "image" not in msg.topic and "snapshot" not in msg.topic:
                raw_topics[msg.topic] = json.loads(payload) if payload.startswith("{") else payload
        except Exception:
            pass

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_message = on_message
        client.connect(broker, port, 10)
        client.subscribe("#")
        client.loop_start()
        time.sleep(timeout)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        raw_topics["mqtt_error"] = str(e)

    # Flatten all received MQTT topics/JSON keys to extract Kobra S1 metrics
    flat_blob = json.dumps(raw_topics).lower()
    parsed = st.session_state["parsed_temps"].copy()

    for topic, val in raw_topics.items():
        t_low = topic.lower()
        val_str = str(val).strip()

        # Parse scalar topics or nested JSON dicts from anycubic-s1-mqtt-bridge
        if isinstance(val, (int, float)) or re.match(r"^\d+(\.\d+)?$", val_str):
            num = int(float(val_str))
            if "nozzle" in t_low or "hotend" in t_low:
                if "target" in t_low:
                    parsed["nozzle_target"] = num
                else:
                    parsed["nozzle_actual"] = num
            elif "bed" in t_low or "hotbed" in t_low:
                if "target" in t_low:
                    parsed["bed_target"] = num
                else:
                    parsed["bed_actual"] = num
            elif "chamber" in t_low or "box_temp" in t_low:
                parsed["chamber_temp"] = num
            elif "progress" in t_low:
                parsed["progress"] = num
        elif isinstance(val, dict):
            # Inspect nested keys inside JSON state payloads
            for k, v in val.items():
                k_low = k.lower()
                if isinstance(v, (int, float)):
                    if k_low in ("nozzle_temp", "curr_nozzle_temp", "hotend_temp"):
                        parsed["nozzle_actual"] = int(v)
                    elif k_low in ("target_nozzle_temp", "nozzle_target"):
                        parsed["nozzle_target"] = int(v)
                    elif k_low in ("bed_temp", "curr_bed_temp", "hotbed_temp"):
                        parsed["bed_actual"] = int(v)
                    elif k_low in ("target_bed_temp", "bed_target"):
                        parsed["bed_target"] = int(v)
                    elif "chamber" in k_low:
                        parsed["chamber_temp"] = int(v)
                    elif "progress" in k_low:
                        parsed["progress"] = int(v)
                elif isinstance(v, str) and k_low in ("state", "status", "print_status"):
                    parsed["state"] = v

    if "busy" in flat_blob or "printing" in flat_blob:
        parsed["state"] = "printing"
    if "drying" in flat_blob and "true" in flat_blob:
        parsed["ace_drying"] = True

    return raw_topics, parsed

# 3. Grab Live Frame from Kobra S1 FLV Stream (Port 18088)
def capture_kobra_flv_frame(printer_ip):
    stream_url = f"http://{printer_ip}:18088/flv"
    cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        return None, f"Could not open stream at {stream_url}. Ensure LAN Mode is ON."
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return None, "Connected to stream, but failed to decode frame."
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb_frame), None

# Sidebar Configuration
st.sidebar.header("🌐 Kobra S1 LAN Sync")
printer_ip = st.sidebar.text_input("Printer LAN IP", value=DEFAULT_IP)
mqtt_host = st.sidebar.text_input("MQTT Bridge IP (UNO Q or Local)", value=MQTT_BROKER)

if st.sidebar.button("🔄 Sync Camera + Live Temps Now", type="primary"):
    with st.sidebar:
        with st.spinner(f"Syncing stream & telemetry from {printer_ip}..."):
            st.session_state["lan_info"] = fetch_lan_discovery(printer_ip)
            raw_mqtt, parsed_temps = fetch_and_parse_telemetry(mqtt_host, MQTT_PORT)
            st.session_state["mqtt_data"] = raw_mqtt
            st.session_state["parsed_temps"] = parsed_temps
            img, err = capture_kobra_flv_frame(printer_ip)
            if img:
                st.session_state["active_image"] = img
                st.success("Synced live frame & telemetry!")
            else:
                st.warning(f"Telemetry synced, camera warning: {err}")

temps = st.session_state["parsed_temps"]

st.sidebar.subheader("⚙️ Active Printer Telemetry")
filament = st.sidebar.selectbox("Filament Type (ACE Pro)", ["PLA", "PETG", "ABS", "ASA", "TPU"])
nozzle_temp = st.sidebar.slider("Nozzle Temp (°C)", 0, 300, int(temps["nozzle_actual"]))
bed_temp = st.sidebar.slider("Bed Temp (°C)", 0, 120, int(temps["bed_actual"]))
chamber_temp = st.sidebar.slider("Chamber Temp (°C)", 0, 70, int(temps["chamber_temp"]))
door_closed = st.sidebar.checkbox("Enclosure Door Closed", value=True)
ace_drying = st.sidebar.checkbox("ACE Pro Dryer Active", value=bool(temps["ace_drying"]))

# Main Dashboard Metrics Row
m1, m2, m3, m4 = st.columns(4)
m1.metric("🔥 Nozzle Temp", f"{nozzle_temp} °C", f"Target: {temps['nozzle_target']} °C")
m2.metric("🛏️ Bed Temp", f"{bed_temp} °C", f"Target: {temps['bed_target']} °C")
m3.metric("🌡️ Chamber Temp", f"{chamber_temp} °C", "Enclosed CoreXY")
m4.metric("🖨️ Printer State", str(temps["state"]).upper(), f"{temps['progress']}% Progress")

st.divider()

col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("📷 Print Inspection Feed")
    source_tab1, source_tab2 = st.tabs(["📡 Live Kobra S1 Stream", "📁 Upload Photo"])

    with source_tab1:
        st.write(f"Stream Endpoint: `http://{printer_ip}:18088/flv`")
        if st.button("📸 Grab Live Snapshot Only"):
            with st.spinner("Connecting to Kobra S1 camera..."):
                img, err = capture_kobra_flv_frame(printer_ip)
                if img:
                    st.session_state["active_image"] = img
                    st.success("Captured live frame from Kobra S1!")
                else:
                    st.error(err)

    with source_tab2:
        uploaded = st.file_uploader("Or upload a print photo", type=["jpg", "jpeg", "png", "webp"])
        if uploaded:
            st.session_state["active_image"] = Image.open(uploaded)

    if "active_image" in st.session_state:
        st.image(st.session_state["active_image"], caption="Current Inspection Frame", use_container_width=True)

with col2:
    st.subheader("🩺 Gemma 4 Diagnostic Console")
    lan_info = st.session_state.get("lan_info", {"note": "Click 'Sync Camera + Live Temps Now' in sidebar"})
    mqtt_data = st.session_state.get("mqtt_data", {})

    with st.expander("View Raw LAN & MQTT Telemetry JSON", expanded=False):
        st.json({"parsed_metrics": temps, "lan_discovery_18910": lan_info, "mqtt_bridge_state": mqtt_data})

    user_notes = st.text_input("Additional observations:", placeholder="e.g., First layer ripples, clicking extruder...")

    if st.button("🩺 Run Gemma 4 Print Diagnosis", type="primary", use_container_width=True):
        if "active_image" not in st.session_state:
            st.warning("Please grab a live snapshot from the printer or upload a photo first.")
        else:
            client = get_ai_client()
            if not client:
                st.error("No API key found in .env! Set GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT.")
            else:
                model_id = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")
                prompt = f"""
                You are the 'Kobra S1 Print Doctor', an expert FDM 3D printing diagnostic system for the Anycubic Kobra S1 (enclosed CoreXY with ACE Pro multi-color dryer unit).

                Live Printer Telemetry ({printer_ip}):
                - Printer Status: {temps['state']} ({temps['progress']}% complete)
                - Nozzle Temperature: {nozzle_temp}°C (Target: {temps['nozzle_target']}°C)
                - Bed Temperature: {bed_temp}°C (Target: {temps['bed_target']}°C)
                - Chamber Temperature: {chamber_temp}°C
                - Filament Loaded: {filament}
                - Enclosure Door Closed: {door_closed}
                - ACE Pro Active Drying: {ace_drying}
                - Raw MQTT Bridge Payload: {json.dumps(mqtt_data)}
                - Operator Notes: {user_notes}

                Inspect the attached camera frame and live telemetry:
                1. **Visual Defect & Print Status**: Identify any defects on the build plate (spaghetti, warping, stringing, under-extrusion, Z-offset issues).
                2. **Kobra S1 Telemetry & Root Cause**: Check if Nozzle ({nozzle_temp}°C), Bed ({bed_temp}°C), and Chamber ({chamber_temp}°C) temperatures match the {filament} filament requirements, or if enclosure heat creep / damp filament in the ACE Pro is causing issues.
                3. **Actionable Slicer & Hardware Fixes**: Provide 3 specific fixes with exact numbers for Anycubic Slicer Next.
                """
                with st.spinner(f"Analyzing Kobra S1 stream & telemetry with {model_id}..."):
                    try:
                        response = client.models.generate_content(
                            model=model_id,
                            contents=[st.session_state["active_image"], prompt]
                        )
                        st.markdown("### 📋 Diagnosis & Slicer Fixes")
                        st.markdown(response.text)
                    except Exception as e:
                        st.error(f"Gemma 4 API Error: {e}")