// routes/me.js
const express = require('express');
const router = express.Router();
const { verifyFirebaseToken, requireDbUser } = require('../middleware/auth');

router.get('/', verifyFirebaseToken, requireDbUser, (req, res) => {
  res.json({
    id: req.dbUser.id,
    email: req.dbUser.email,
    role: req.dbUser.role,
    full_name: req.dbUser.full_name,
  });
});

module.exports = router;
