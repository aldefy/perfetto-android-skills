-- Was the app under test built debuggable? debuggable = 1 means every timing in
-- this trace is 2 to 5x slower than a release build and is directional only.
-- Needs the android.packages_list data source (assets/startup_jank.pbtxt has it).
-- No rows means the data source was missing or the package filter matched nothing.
SELECT package_name, version_code, debuggable
FROM package_list
WHERE package_name GLOB '__PKG__'
ORDER BY package_name
LIMIT 50;
