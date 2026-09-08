"use client";

import { useState } from "react";

export function SignOut() {
  const [pending, setPending] = useState(false);

  async function signOut() {
    setPending(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      // Full navigation so the server sees the cleared cookie.
      window.location.href = "/login";
    }
  }

  return (
    <button className="signout" onClick={signOut} disabled={pending}>
      {pending ? "Signing out…" : "Sign out"}
    </button>
  );
}
