import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(<App />);

// Installable app + offline shell. Browsers only allow this on https or localhost;
// on the Pi's plain-http hotspot, "Add to Home screen" still works as a shortcut.
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
