import { useState } from "react";
import { CaptureScreen } from "./components/CaptureScreen";
import { Gallery } from "./components/Gallery";
import { OutboxStatus } from "./components/OutboxStatus";

export default function App() {
  const [tab, setTab] = useState<"capture" | "gallery">("capture");

  return (
    <div className="app">
      <header className="app-header">
        <span className="app-title">SwarmLens</span>
        <OutboxStatus />
      </header>

      <main className="app-main">{tab === "capture" ? <CaptureScreen /> : <Gallery />}</main>

      <nav className="tab-bar">
        <button className={tab === "capture" ? "active" : ""} onClick={() => setTab("capture")}>
          Capture
        </button>
        <button className={tab === "gallery" ? "active" : ""} onClick={() => setTab("gallery")}>
          Gallery
        </button>
      </nav>
    </div>
  );
}
