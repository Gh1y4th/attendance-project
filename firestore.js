// firestore.js
const admin = require('firebase-admin');

function getDb() {
  return admin.firestore();
}

async function getDocsByIds(collectionName, ids) {
  const db = getDb();
  const uniqueIds = [...new Set(ids)].filter(Boolean);
  if (uniqueIds.length === 0) return {};

  const refs = uniqueIds.map((id) => db.collection(collectionName).doc(String(id)));
  const snaps = await db.getAll(...refs);

  const map = {};
  snaps.forEach((snap) => {
    if (snap.exists) map[snap.id] = snap.data();
  });
  return map;
}

module.exports = { getDb, getDocsByIds };
