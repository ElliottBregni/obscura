import { describe, it, expect } from "vitest";
import { withProfileId } from "../src/sidepanel/messaging.js";

describe("withProfileId", () => {
  it("attaches profile_id when provided", () => {
    expect(withProfileId({ type: "send", id: "x" }, "abc")).toEqual({
      type: "send",
      id: "x",
      profile_id: "abc",
    });
  });

  it("leaves message unchanged when profileId is falsy", () => {
    const msg = { type: "ping", id: "boot" };
    expect(withProfileId(msg, null)).toBe(msg);
    expect(withProfileId(msg, "")).toBe(msg);
    expect(withProfileId(msg, undefined)).toBe(msg);
  });

  it("does not mutate the input when tagging", () => {
    const msg = { type: "send" };
    withProfileId(msg, "abc");
    expect(msg).toEqual({ type: "send" });
  });
});
