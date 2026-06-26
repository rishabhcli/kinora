// Dev-only entry for the icon gallery (served by `vite` dev at /icon-gallery.html;
// not part of the production build, whose only input is index.html).
import { createRoot } from "react-dom/client";
import IconGallery from "../components/icons/IconGallery";
import "../index.css";

createRoot(document.getElementById("root")!).render(<IconGallery />);
