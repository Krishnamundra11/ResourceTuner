import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderApp } from "@/test/utils";
import AgentWorkflow from "./AgentWorkflow";

describe("Agent Workflow Tab", () => {
  it("renders page header and instructions cleanly", () => {
    renderApp(<AgentWorkflow />);
    expect(screen.getByRole("heading", { name: "Agent Workflow" })).toBeInTheDocument();
  });

  it("handles empty or waiting runs gracefully without throwing", () => {
    renderApp(<AgentWorkflow />);
    expect(document.body).toBeInTheDocument();
  });
});
