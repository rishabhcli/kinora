import { test, expect } from "../fixtures/test";

// PDF upload flow. The standalone library harness mounts the real LibraryPage
// (and its UploadBook), whose hidden <input type="file"> we drive directly. The
// API mock answers POST /api/books with an "importing" book, so the upload list
// renders deterministically without the FastAPI stack. KINORA_LIVE_VIDEO stays
// OFF throughout — upload only triggers ingest, never live Wan.

test.describe("upload", () => {
  test("the upload affordance is present in the library", async ({ library, upload }) => {
    await library.openHarness();
    await upload.expectAffordanceVisible();
    await expect(upload.fileInput.first()).toBeAttached();
  });

  test("selecting a PDF adds it to the uploads-in-progress list", async ({
    library,
    upload,
  }) => {
    await library.openHarness();
    await upload.uploadBuffer("moby-dick.pdf");
    // The uploads list appears once an item is queued.
    await expect(upload.uploadList).toBeVisible({ timeout: 15_000 });
    await expect(upload.uploadList.getByRole("listitem").first()).toBeVisible();
  });

  test("the uploaded item surfaces a status (uploading / importing / ready)", async ({
    library,
    upload,
  }) => {
    await library.openHarness();
    await upload.uploadBuffer("treasure-island.pdf");
    const item = upload.uploadList.getByRole("listitem").first();
    await expect(item).toBeVisible({ timeout: 15_000 });
    await expect(item).toContainText(/Uploading|Importing|Ready|queued|importing/i, {
      timeout: 15_000,
    });
  });
});
