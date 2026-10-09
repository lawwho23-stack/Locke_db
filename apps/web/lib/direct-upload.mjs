/**
 * @template T
 * @param {() => Promise<unknown>} uploadFile
 * @param {() => Promise<T>} finalize
 * @returns {Promise<T>}
 */
export async function completeDirectUpload(uploadFile, finalize) {
  // A rejected upload can still have stored bytes. Ask the same receipt to finish.
  try {
    await uploadFile();
  } catch {
    /* Finalization checks whether bytes exist. */
  }
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      return await finalize();
    } catch (failure) {
      const status = failure?.status;
      if (attempt === 1 || (status >= 400 && status < 500 && status !== 409))
        throw failure;
    }
  }
  throw new Error("Unable to finalize upload.");
}
