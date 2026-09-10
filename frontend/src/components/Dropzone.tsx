"use client";

import { IconAlertTriangle, IconCloudUpload, IconFileText } from "@tabler/icons-react";
import { useCallback, useRef, useState } from "react";

import { ApiError, api, type UploadResponse } from "@/lib/api";
import { formatBytes } from "@/lib/utils";

const MAX_BYTES = 25 * 1024 * 1024;

interface DropzoneProps {
  onUploaded: (result: UploadResponse) => void;
  disabled?: boolean;
}

export function Dropzone({ onUploaded, disabled = false }: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  const handleFile = useCallback(
    async (file: File | undefined) => {
      if (!file) return;
      setError(null);
      setWarnings([]);

      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setError("Only PDF files are supported.");
        return;
      }
      if (file.size === 0) {
        setError("That file is empty.");
        return;
      }
      if (file.size > MAX_BYTES) {
        setError(`That file is ${formatBytes(file.size)}. The limit is 25 MB.`);
        return;
      }

      setBusy(true);
      try {
        const result = await api.uploadDocument(file);
        setWarnings(result.warnings ?? []);
        onUploaded(result);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Upload failed. Please try again.");
      } finally {
        setBusy(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [onUploaded],
  );

  return (
    <div>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled && !busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (disabled || busy) return;
          void handleFile(event.dataTransfer.files?.[0]);
        }}
        className={`card card-body text-center ${dragging ? "tabler-dropzone-active" : ""} ${
          disabled || busy ? "opacity-50" : ""
        }`}
        style={{ borderStyle: "dashed" }}
      >
        <div className="mx-auto mb-2">
          <span className="avatar avatar-md rounded bg-blue-lt">
            {busy ? (
              <span className="spinner-border spinner-border-sm" role="status" />
            ) : (
              <IconCloudUpload size={20} />
            )}
          </span>
        </div>
        {busy ? (
          <div>
            <p className="mb-1 fw-medium">Parsing, indexing and extracting…</p>
            <p className="text-muted mb-2">This can take up to a minute.</p>
            <div className="progress progress-sm mx-auto" style={{ maxWidth: 220 }}>
              <div className="progress-bar progress-bar-indeterminate" />
            </div>
          </div>
        ) : (
          <div>
            <p className="mb-1 fw-medium">Drag a PDF here</p>
            <p className="text-muted mb-3">Bills, policies, warranties, rental or vehicle records</p>
            <button
              type="button"
              disabled={disabled}
              onClick={() => inputRef.current?.click()}
              className="btn btn-primary btn-sm"
            >
              <IconFileText size={16} className="me-1" />
              Choose PDF file
            </button>
            <p className="text-muted mt-2 mb-0">≤25 MB · needs a %PDF- header</p>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          hidden
          disabled={disabled || busy}
          onChange={(event) => void handleFile(event.target.files?.[0])}
        />
      </div>

      {error ? (
        <div className="alert alert-danger mt-2 mb-0" role="alert">
          <IconAlertTriangle size={16} className="me-2" />
          {error}
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="alert alert-warning mt-2 mb-0" role="alert">
          <div>
            <div className="fw-medium mb-1">Saved with warnings</div>
            <ul className="mb-0 ps-3">
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
