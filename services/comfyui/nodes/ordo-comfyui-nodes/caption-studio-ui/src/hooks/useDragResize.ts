import { useCallback, useRef } from "react";

interface UseDragOpts {
  onDrag: (dx: number) => void;
  onDragEnd: (wasDragged: boolean) => void;
}

export function useDrag({ onDrag, onDragEnd }: UseDragOpts) {
  const wasDraggedRef = useRef(false);

  const startDrag = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();

      wasDraggedRef.current = false;
      const startX = e.clientX;

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        if (Math.abs(dx) > 3) wasDraggedRef.current = true;
        onDrag(dx);
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        onDragEnd(wasDraggedRef.current);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [onDrag, onDragEnd]
  );

  return { startDrag };
}

interface UseResizeOpts {
  onResize: (dx: number) => void;
  onResizeEnd: () => void;
}

export function useResize({ onResize, onResizeEnd }: UseResizeOpts) {
  const startResize = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();

      const startX = e.clientX;

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        onResize(dx);
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        onResizeEnd();
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [onResize, onResizeEnd]
  );

  return { startResize };
}
