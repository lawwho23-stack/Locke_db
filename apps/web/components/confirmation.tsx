"use client";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import Icon from "./icon";
type Confirm = (message: string) => Promise<boolean>;
const Context = createContext<Confirm | null>(null);
export function useConfirm() {
  const confirm = useContext(Context);
  if (!confirm) throw new Error("Confirmation provider required.");
  return confirm;
}
export default function ConfirmationProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [message, setMessage] = useState("");
  const resolver = useRef<((answer: boolean) => void) | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  function finish(answer: boolean) {
    resolver.current?.(answer);
    resolver.current = null;
    dialog.current?.close();
    setMessage("");
  }
  useEffect(() => {
    if (message) dialog.current?.showModal();
  }, [message]);
  useEffect(() => () => resolver.current?.(false), []);
  const confirm: Confirm = (text) => {
    resolver.current?.(false);
    setMessage(text);
    return new Promise((resolve) => {
      resolver.current = resolve;
    });
  };
  return (
    <Context.Provider value={confirm}>
      {children}
      <dialog
        ref={dialog}
        className="confirmation"
        aria-labelledby="confirm-title"
        onCancel={(event) => {
          event.preventDefault();
          finish(false);
        }}
      >
        <div className="dialog-heading">
          <h2 id="confirm-title">Confirm action</h2>
          <button
            className="icon-button secondary"
            aria-label="Close confirmation"
            onClick={() => finish(false)}
          >
            <Icon name="close" />
          </button>
        </div>
        <p>{message}</p>
        <div className="actions">
          <button className="secondary" autoFocus onClick={() => finish(false)}>
            Cancel
          </button>
          <button className="danger" onClick={() => finish(true)}>
            Confirm action
          </button>
        </div>
      </dialog>
    </Context.Provider>
  );
}
