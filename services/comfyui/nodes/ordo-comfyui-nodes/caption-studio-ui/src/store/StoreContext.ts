import { createContext, useContext } from "react";
import { useStore } from "zustand";
import type { CaptionStore, CaptionStoreState } from "./useCaptionStore";

export const StoreContext = createContext<CaptionStore | null>(null);

export function useCaptionStoreContext<T>(
  selector: (state: CaptionStoreState) => T
): T {
  const store = useContext(StoreContext);
  if (!store) {
    throw new Error("useCaptionStoreContext must be used within StoreContext.Provider");
  }
  return useStore(store, selector);
}
