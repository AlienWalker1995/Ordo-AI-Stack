import PreviewCanvas from "./PreviewCanvas";
import TransportControls from "./TransportControls";

export default function VideoPreviewPanel() {
  return (
    <div className="jcs:flex jcs:flex-col jcs:gap-1 jcs:p-2 jcs:items-center jcs:border-b jcs:border-border">
      <div className="jcs:flex jcs:justify-center jcs:w-full">
        <PreviewCanvas />
      </div>
      <p className="jcs:text-[9px] jcs:text-text-dim jcs:text-center jcs:m-0">
        Canvas preview is approximate. Actual rendering uses Pillow with exact fonts.
      </p>
      <TransportControls />
    </div>
  );
}
