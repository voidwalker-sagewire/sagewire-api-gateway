from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

SERVICES = {
    "tts": "https://tts.sagewire.dev",
    "stt": "https://stt.sagewire.dev",
    "loc": "https://loc.sagewire.dev",
    "wx":  "https://weather.herdmate.ag"
}


@app.route("/health")
def health():
    return jsonify({
        "service": "sagewire-api-gateway",
        "status": "ok",
        "version": "1.0"
    })


@app.route("/services")
def services():
    return jsonify(SERVICES)


@app.route("/tts/speak", methods=["POST"])
def tts():
    r = requests.post(
        SERVICES["tts"] + "/speak",
        json=request.get_json(force=True)
    )

    return (
        r.content,
        r.status_code,
        {"Content-Type": r.headers.get("Content-Type", "application/octet-stream")}
    )


@app.route("/stt/transcribe", methods=["POST"])
def stt():

    if "audio" not in request.files:
        return jsonify({"error": "audio file required"}), 400

    audio = request.files["audio"]

    r = requests.post(
        SERVICES["stt"] + "/transcribe",
        files={
            "audio": (
                audio.filename,
                audio.stream,
                audio.content_type
            )
        }
    )

    return (
        r.content,
        r.status_code,
        {"Content-Type": r.headers.get("Content-Type", "application/json")}
    )


@app.route("/loc/stamp", methods=["POST"])
def loc():

    r = requests.post(
        SERVICES["loc"] + "/stamp",
        json=request.get_json(force=True)
    )

    return (
        r.content,
        r.status_code,
        {"Content-Type": r.headers.get("Content-Type", "application/json")}
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5010)
