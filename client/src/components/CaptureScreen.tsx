import { useEffect, useRef, useState } from "react";
import { db } from "../db";
import { tick } from "../vclock";
import { currentGuestId } from "../guest";
import { syncOutbox } from "../sync";
import { ZONES } from "../constants";

export function CaptureScreen() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [zone, setZone] = useState(ZONES[0]);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    let stream: MediaStream | null = null;
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then((s) => {
        stream = s;
        if (videoRef.current) videoRef.current.srcObject = s;
      })
      .catch((e) => setCameraError(String(e)));
    return () => stream?.getTracks().forEach((t) => t.stop());
  }, []);

  async function capture() {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || video.videoWidth === 0) return;

    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")!.drawImage(video, 0, 0);
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", 0.85),
    );
    if (!blob) return;

    // Write to the outbox immediately and render optimistically -- the
    // shutter must never wait on a network round trip. Sync happens
    // fire-and-forget, in the background, per ROADMAP.md's Phase 5 spec.
    await db.outbox.add({
      local_id: crypto.randomUUID(),
      kind: "photo",
      guest_id: currentGuestId(),
      zone,
      composition_score: 0,
      vclock: tick(),
      blob,
      created_at: Date.now(),
      synced: 0,
    });
    void syncOutbox();

    setFlash(true);
    setTimeout(() => setFlash(false), 150);
  }

  return (
    <div className="capture-screen">
      {cameraError ? (
        <div className="camera-error">
          Camera unavailable: {cameraError}
          <br />
          (needs HTTPS or localhost, and camera permission)
        </div>
      ) : (
        <video ref={videoRef} autoPlay playsInline muted className="viewfinder" />
      )}
      <canvas ref={canvasRef} style={{ display: "none" }} />
      {flash && <div className="flash" />}

      <div className="capture-controls">
        <select value={zone} onChange={(e) => setZone(e.target.value)} className="zone-picker">
          {ZONES.map((z) => (
            <option key={z} value={z}>
              {z.replace("_", " ")}
            </option>
          ))}
        </select>
        <button className="shutter" onClick={capture} disabled={!!cameraError} aria-label="Capture photo" />
      </div>
    </div>
  );
}
