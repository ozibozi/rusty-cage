use std::{
    fs::OpenOptions,
    io::Read,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};
use std::os::unix::fs::OpenOptionsExt;

#[cfg(target_os = "macos")]
const O_NOFOLLOW: i32 = 0x0100;
#[cfg(target_os = "linux")]
const O_NOFOLLOW: i32 = 0x20000;

pub fn acceptance_text_is_valid(text: &str) -> bool {
    let versions: Vec<_> = text
        .lines()
        .map(str::trim)
        .filter(|line| line.starts_with("EULA_VERSION"))
        .collect();
    let acceptances: Vec<_> = text
        .lines()
        .map(str::trim)
        .filter(|line| line.starts_with("PROPRIETARY_COMPONENTS_ACCEPTED"))
        .collect();
    versions == ["EULA_VERSION = 1"]
        && acceptances == ["PROPRIETARY_COMPONENTS_ACCEPTED = true"]
}

pub fn require_acceptance_at(root: &Path) -> Result<(), &'static str> {
    let path = root.join("EULA.md");
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(path)
        .map_err(|_| "Review EULA.md and enable proprietary-component acceptance before use.")?;
    if !file
        .metadata()
        .map_err(|_| "EULA.md is unreadable.")?
        .is_file()
    {
        return Err("EULA.md must be a regular file.");
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|_| "EULA.md is unreadable.")?;
    let text = String::from_utf8(bytes).map_err(|_| "EULA.md is not valid UTF-8.")?;
    if !acceptance_text_is_valid(&text) {
        return Err("Proprietary component use is disabled; review EULA.md and enable its current acceptance setting.");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::symlink};

    fn temporary_directory() -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "bedrock-consent-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&directory).unwrap();
        directory
    }

    #[test]
    fn acceptance_requires_exact_unique_current_settings() {
        assert!(acceptance_text_is_valid(
            "EULA_VERSION = 1\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n"
        ));
        assert!(!acceptance_text_is_valid(
            "EULA_VERSION = 1\nPROPRIETARY_COMPONENTS_ACCEPTED = false\n"
        ));
        assert!(!acceptance_text_is_valid(
            "EULA_VERSION = 0\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n"
        ));
        assert!(!acceptance_text_is_valid(
            "EULA_VERSION = 1\nPROPRIETARY_COMPONENTS_ACCEPTED = false\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n"
        ));
        assert!(!acceptance_text_is_valid(
            "EULA_VERSION = 1\nEULA_VERSION = 1\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n"
        ));
    }

    #[test]
    fn acceptance_reader_rejects_missing_and_symlinked_files() {
        let directory = temporary_directory();
        assert!(require_acceptance_at(&directory).is_err());
        let target = directory.join("target");
        fs::write(
            &target,
            "EULA_VERSION = 1\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n",
        )
        .unwrap();
        symlink(&target, directory.join("EULA.md")).unwrap();
        assert!(require_acceptance_at(&directory).is_err());
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn acceptance_reader_accepts_explicit_user_setting() {
        let directory = temporary_directory();
        fs::write(
            directory.join("EULA.md"),
            "EULA_VERSION = 1\nPROPRIETARY_COMPONENTS_ACCEPTED = true\n",
        )
        .unwrap();
        assert!(require_acceptance_at(&directory).is_ok());
        fs::remove_dir_all(directory).unwrap();
    }
}
