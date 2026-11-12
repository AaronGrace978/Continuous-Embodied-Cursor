// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";
// No frames, images or video are sent across IPC or to a model provider.
class HandCamera {
  constructor({ video, canvas, onSample, onLandmarks, onStatus, onError }) {
    Object.assign(this, {
      video,
      canvas,
      onSample,
      onLandmarks,
      onStatus,
      onError,
    });
    this.active = false;
    this.generation = 0;
    this.detector = null;
    this.stream = null;
    this.inflight = false;
  }
  async start() {
    const generation = ++this.generation;
    this.active = true;
    try {
      this.onStatus("Requesting camera access…");
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 640 },
          height: { ideal: 480 },
          frameRate: { ideal: 30, max: 30 },
        },
        audio: false,
      });
      if (!this.active || generation !== this.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return false;
      }
      this.stream = stream;
      this.video.srcObject = stream;
      stream.getVideoTracks()[0].addEventListener("ended", () => {
        if (this.active)
          this.onError(
            new Error("Camera disconnected. Enable it again to resume."),
          );
      });
      await this.video.play();
      this.onStatus("Loading hand model…");
      if (!this.detector) {
        const { HandLandmarker, FilesetResolver } = await import(
          "./vendor/vision/vision_bundle.mjs"
        );
        const files = await FilesetResolver.forVisionTasks(
          new URL("./vendor/vision/wasm", location.href).href,
        );
        if (!this.active || generation !== this.generation) return false;
        const detector = await HandLandmarker.createFromOptions(files, {
          baseOptions: {
            modelAssetPath:
              "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
          },
          runningMode: "VIDEO",
          numHands: 2,
          minHandDetectionConfidence: 0.65,
          minTrackingConfidence: 0.65,
        });
        if (!this.active || generation !== this.generation) {
          detector.close();
          return false;
        }
        this.detector = detector;
      }
      if (!this.active || generation !== this.generation) return false;
      this.lastVideoTime = -1;
      this.onStatus("Show an open hand");
      return true;
    } catch (error) {
      if (generation !== this.generation) return false;
      this.stop();
      throw new Error(
        error.name === "NotAllowedError"
          ? "Camera permission was denied. Allow camera access in your system settings and retry."
          : error.name === "NotFoundError"
            ? "No camera found. Connect a webcam and retry."
            : "Could not start hand tracking. Check your camera and internet connection, then retry.",
      );
    }
  }
  track() {
    if (!this.active) return;
    this.frame = setTimeout(() => this.track(), 1000 / 30);
    if (
      this.inflight ||
      this.video.readyState < 2 ||
      this.video.currentTime === this.lastVideoTime
    )
      return;
    this.lastVideoTime = this.video.currentTime;
    try {
      const result = this.detector.detectForVideo(
        this.video,
        performance.now(),
      );
      const hands = result.landmarks.map((points, index) => ({
        id: result.handedness?.[index]?.[0]?.categoryName || String(index),
        points,
      }));
      // Keep the chosen pointer hand stable if a second hand enters the frame.
      const primary =
        hands.find((hand) => hand.id === this.primaryHand) || hands[0];
      this.primaryHand = primary?.id;
      const points = primary?.points;
      this.draw(points);
      let sample = null;
      if (points) {
        const distance = (a, b) =>
          Math.hypot(
            a.x - b.x,
            ((a.y - b.y) * this.video.videoHeight) / this.video.videoWidth,
          );
        const palm = distance(points[0], points[9]);
        if (palm > 0.025)
          sample = {
            x: Math.min(1, Math.max(0, 1 - points[8].x)),
            y: Math.min(1, Math.max(0, points[8].y)),
            pinch: Math.min(5, distance(points[4], points[8]) / palm),
            gesture: AirKeyboard.gesture(
              points,
              this.video.videoHeight / this.video.videoWidth,
            ),
          };
      }
      this.inflight = true;
      const generation = this.generation;
      Promise.resolve(
        this.onLandmarks?.(
          hands,
          performance.now(),
          this.video.videoHeight / this.video.videoWidth,
        ),
      )
        .then(() => {
          if (this.active && generation === this.generation)
            return this.onSample(sample);
        })
        .catch((error) => {
          if (this.active) this.onError(error);
        })
        .finally(() => {
          this.inflight = false;
        });
    } catch (error) {
      this.onError(
        new Error("Hand tracking stopped. Disable the camera and retry."),
      );
    }
  }
  draw(points) {
    const canvas = this.canvas,
      ctx = canvas.getContext("2d");
    if (
      canvas.width !== this.video.videoWidth ||
      canvas.height !== this.video.videoHeight
    ) {
      canvas.width = this.video.videoWidth;
      canvas.height = this.video.videoHeight;
    }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (this.typingArea) {
      const area = this.typingArea;
      for (const key of AirKeyboard.keys) {
        const x = (area.x + key.x * area.w) * canvas.width;
        const y = (area.y + key.y * area.h) * canvas.height;
        ctx.strokeStyle = "#8ce6ff";
        ctx.strokeRect(
          x,
          y,
          key.w * area.w * canvas.width,
          key.h * area.h * canvas.height,
        );
        ctx.fillStyle = "#ffffff";
        ctx.font = "10px monospace";
        ctx.fillText(key.label || key.id, x + 3, y + 13);
      }
    }
    if (!points) return;
    for (const index of [0, 4, 8, 9, 12, 16, 20]) {
      ctx.beginPath();
      ctx.arc(
        (1 - points[index].x) * canvas.width,
        points[index].y * canvas.height,
        index === 8 ? 10 : 5,
        0,
        Math.PI * 2,
      );
      ctx.fillStyle = index === 8 ? "#ff875b" : "#9deaff";
      ctx.fill();
    }
    ctx.beginPath();
    ctx.moveTo((1 - points[4].x) * canvas.width, points[4].y * canvas.height);
    ctx.lineTo((1 - points[8].x) * canvas.width, points[8].y * canvas.height);
    ctx.strokeStyle = "#ff875b";
    ctx.lineWidth = 3;
    ctx.stroke();
  }
  stop() {
    this.active = false;
    this.generation++;
    clearTimeout(this.frame);
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.video.srcObject = null;
    this.canvas
      .getContext("2d")
      .clearRect(0, 0, this.canvas.width, this.canvas.height);
  }
}
