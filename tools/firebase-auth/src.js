// Bundled into services/web/app/static/firebase-auth.js so the page loads no script from another server.
// Rebuild: cd tools/firebase-auth && npm install && npm run build
import { initializeApp } from "firebase/app";
import { getAuth, GoogleAuthProvider, signInWithPopup } from "firebase/auth";

export async function signIn(config) {
  const app = initializeApp(config);
  const auth = getAuth(app);
  const result = await signInWithPopup(auth, new GoogleAuthProvider());
  return result.user.getIdToken();
}
