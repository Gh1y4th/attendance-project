// routes/dev-seed.js
// TEMPORARY, ONE-TIME USE ONLY. Delete after use.
const express = require('express');
const router = express.Router();
const admin = require('firebase-admin');
const { getDb } = require('../firestore');

router.get('/seed', async (req, res) => {
  if (req.query.key !== process.env.PYTHON_SERVICE_API_KEY) {
    return res.status(401).json({ error: 'Invalid key' });
  }

  const db = getDb();
  const ADMIN_EMAIL = 'ghiyath2011hk@gmail.com';
  const ADMIN_PASSWORD = '2011201820112018';

  try {
    let adminAuthUser;
    try {
      adminAuthUser = await admin.auth().getUserByEmail(ADMIN_EMAIL);
    } catch (err) {
      if (err.code === 'auth/user-not-found') {
        adminAuthUser = await admin.auth().createUser({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD });
      } else {
        throw err;
      }
    }

    await db.collection('users').doc(adminAuthUser.uid).set(
      {
        email: ADMIN_EMAIL,
        role: 'dev',
        full_name: 'Ghiyath',
        password: ADMIN_PASSWORD,
        is_active: true,
        createdAt: new Date(),
      },
      { merge: true }
    );

    res.json({ success: true, message: 'Admin account created/confirmed. You can now log in.' });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
