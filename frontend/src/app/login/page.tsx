"use client";

import { IconFileText } from "@tabler/icons-react";
import { useRouter } from "next/navigation";
import type { FormEvent } from "react";
import { useState } from "react";

import { ApiError, api, setToken } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null);

    if (mode === "signup" && password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setBusy(true);
    try {
      const response =
        mode === "signup"
          ? await api.signup(email.trim(), password)
          : await api.login(email.trim(), password);
      setToken(response.access_token);
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
      setBusy(false);
    }
  }

  return (
    <div className="page page-center">
      <div className="container container-tight py-4" style={{ maxWidth: 400 }}>
        <div className="text-center mb-4">
          <span className="avatar avatar-md mb-2 rounded" style={{ background: "var(--tblr-primary)" }}>
            <IconFileText size={22} color="#fff" />
          </span>
          <h1 className="h3 mb-1">LifeOS Agent</h1>
          <p className="text-muted">Your documents, turned into information and action.</p>
        </div>

        <div className="card card-md">
          <div className="card-body">
            <div className="btn-group w-100 mb-3" role="group">
              {(["login", "signup"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => {
                    setMode(option);
                    setError(null);
                  }}
                  className={`btn ${mode === option ? "btn-primary" : ""}`}
                >
                  {option === "login" ? "Sign in" : "Create account"}
                </button>
              ))}
            </div>

            <form onSubmit={submit}>
              <div className="mb-3">
                <label className="form-label" htmlFor="email">
                  Email
                </label>
                <input
                  id="email"
                  type="email"
                  required
                  autoComplete="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  className="form-control"
                />
              </div>
              <div className="mb-3">
                <label className="form-label" htmlFor="password">
                  Password
                </label>
                <input
                  id="password"
                  type="password"
                  required
                  autoComplete={mode === "signup" ? "new-password" : "current-password"}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={mode === "signup" ? "At least 8 characters" : "Your password"}
                  className="form-control"
                />
              </div>

              {error ? (
                <div className="alert alert-danger" role="alert">
                  {error}
                </div>
              ) : null}

              <button type="submit" disabled={busy} className="btn btn-primary w-100">
                {busy ? (
                  <span className="spinner-border spinner-border-sm me-2" role="status" />
                ) : null}
                {mode === "login" ? "Sign in" : "Create account"}
              </button>
            </form>
          </div>
        </div>

      </div>
    </div>
  );
}
