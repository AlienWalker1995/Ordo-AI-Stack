import { useCaptionStoreContext } from "./store/StoreContext";
import VideoPreviewPanel from "./components/preview/VideoPreviewPanel";
import StyleBar from "./components/style/StyleBar";
import ExpandableStyleSection from "./components/style/ExpandableStyleSection";
import Timeline from "./components/timeline/Timeline";
import WordEditorBar from "./components/overrides/WordEditorBar";
import "./globals.css";

export default function App() {
  const selectedWordIndex = useCaptionStoreContext((s) => s.selectedWordIndex);
  const styleExpanded = useCaptionStoreContext((s) => s.styleExpanded);

  return (
    <div className="jcs-root">
      {/* Video Preview — hero element */}
      <VideoPreviewPanel />

      {/* Style Controls — compact 2-row toolbar */}
      <StyleBar />
      {styleExpanded && <ExpandableStyleSection />}

      {/* Timeline — always visible */}
      <Timeline />

      {/* Word Editor — appears when word selected */}
      {selectedWordIndex !== null && <WordEditorBar />}
    </div>
  );
}
