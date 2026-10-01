export const dynamic = "force-dynamic";

import { PageHeader } from "@/components/ui";
import { Playground } from "@/components/Playground";

export default function PlaygroundPage() {
  return (
    <>
      <PageHeader
        title="Playground"
        subtitle="Look back: replay a settled campaign with other settings, on the same month's real prices, and see what would have happened. Look ahead: simulate the campaign trading now across a thousand months drawn from NIFTY's own history, and compare plans before you commit to one. Nothing here changes a running test."
      />
      <Playground />
    </>
  );
}
