"""
Package manifest system for Cpyte package extensions.

This module handles parsing and validation of package manifests that declare
extension capabilities like keywords, operators, tags, and parser hooks.
"""

import json
import os
from typing import Dict, List, Optional, Set, Any
from dataclasses import dataclass, field


@dataclass
class CapabilityDeclaration:
    """Represents a package's extension capabilities."""
    keywords: Set[str] = field(default_factory=set)
    operators: Set[str] = field(default_factory=set)
    tags: Set[str] = field(default_factory=set)
    macros: Set[str] = field(default_factory=set)
    custom_types: Set[str] = field(default_factory=set)


@dataclass
class ExtensionHooks:
    """Represents extension hook files provided by a package."""
    parser_hooks: List[str] = field(default_factory=list)
    semantic_hooks: List[str] = field(default_factory=list)
    codegen_hooks: List[str] = field(default_factory=list)
    runtime_hooks: List[str] = field(default_factory=list)


@dataclass
class PackageManifest:
    """Complete package manifest with metadata and capabilities."""
    name: str
    version: str
    capabilities: CapabilityDeclaration = field(default_factory=CapabilityDeclaration)
    extensions: ExtensionHooks = field(default_factory=ExtensionHooks)
    dependencies: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Path information
    package_dir: Optional[str] = None
    manifest_path: Optional[str] = None


class ManifestError(Exception):
    """Raised when manifest parsing or validation fails."""
    pass


class ManifestValidator:
    """Validates package manifests for correctness and security."""
    
    # Reserved keywords that packages cannot override
    RESERVED_KEYWORDS = {
        'def', 'class', 'return', 'if', 'else', 'elif',
        'while', 'for', 'in', 'break', 'continue',
        'public', 'private', 'static', 'virtual', 'override',
        'import', 'true', 'false', 'null', 'True', 'False',
        'and', 'or', 'not', 'print', 'input', 'input_str',
        'switch', 'case', 'default', 'new', 'struct', 'sizeof',
        'ref', 'int64', 'uint64', 'let', 'try', 'except',
        'raise', 'asm',
    }
    
    # Reserved operators that packages cannot override
    RESERVED_OPERATORS = {
        '+', '-', '*', '/', '//', '%', '**',
        '==', '!=', '<', '>', '<=', '>=',
        '&', '|', '^', '~', '<<', '>>',
        '&&', '||', '!',
        '=', '+=', '-=', '*=', '/=', '//=',
        '->', '.', '[', ']', '(', ')', '{', '}',
        ',', ':', ';',
    }
    
    @classmethod
    def validate_manifest(cls, manifest: PackageManifest) -> List[str]:
        """
        Validate a package manifest and return list of validation errors.
        
        Args:
            manifest: The package manifest to validate
            
        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        
        # Check for reserved keyword conflicts
        keyword_conflicts = manifest.capabilities.keywords & cls.RESERVED_KEYWORDS
        if keyword_conflicts:
            errors.append(
                f"Package '{manifest.name}' attempts to override reserved keywords: "
                f"{', '.join(sorted(keyword_conflicts))}"
            )
        
        # Check for reserved operator conflicts
        operator_conflicts = manifest.capabilities.operators & cls.RESERVED_OPERATORS
        if operator_conflicts:
            errors.append(
                f"Package '{manifest.name}' attempts to override reserved operators: "
                f"{', '.join(sorted(operator_conflicts))}"
            )
        
        # Validate keyword format (must be valid identifiers)
        for keyword in manifest.capabilities.keywords:
            if not cls._is_valid_identifier(keyword):
                errors.append(
                    f"Package '{manifest.name}' has invalid keyword '{keyword}': "
                    "must be a valid identifier"
                )
        
        # Validate operator format (must be non-empty strings)
        for operator in manifest.capabilities.operators:
            if not operator or not isinstance(operator, str):
                errors.append(
                    f"Package '{manifest.name}' has invalid operator: '{operator}'"
                )
        
        # Validate tag format (should start with @)
        for tag in manifest.capabilities.tags:
            if not tag.startswith('@'):
                errors.append(
                    f"Package '{manifest.name}' has invalid tag '{tag}': "
                    "tags must start with '@'"
                )
            if not cls._is_valid_identifier(tag[1:]):
                errors.append(
                    f"Package '{manifest.name}' has invalid tag '{tag}': "
                    "tag name must be a valid identifier"
                )
        
        # Validate hook file paths exist if package_dir is provided
        if manifest.package_dir:
            all_hooks = (
                manifest.extensions.parser_hooks +
                manifest.extensions.semantic_hooks +
                manifest.extensions.codegen_hooks +
                manifest.extensions.runtime_hooks
            )
            for hook in all_hooks:
                hook_path = os.path.join(manifest.package_dir, hook)
                if not os.path.exists(hook_path):
                    errors.append(
                        f"Package '{manifest.name}' hook file not found: {hook}"
                    )
        
        return errors
    
    @staticmethod
    def _is_valid_identifier(name: str) -> bool:
        """Check if a string is a valid identifier."""
        if not name:
            return False
        if not (name[0].isalpha() or name[0] == '_'):
            return False
        return all(c.isalnum() or c == '_' for c in name)


class ManifestParser:
    """Parses package manifest files into PackageManifest objects."""
    
    @staticmethod
    def parse_file(manifest_path: str) -> PackageManifest:
        """
        Parse a package manifest file.
        
        Args:
            manifest_path: Path to the manifest JSON file
            
        Returns:
            Parsed PackageManifest object
            
        Raises:
            ManifestError: If parsing fails
        """
        if not os.path.exists(manifest_path):
            raise ManifestError(f"Manifest file not found: {manifest_path}")
        
        try:
            with open(manifest_path, 'r') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ManifestError(f"Invalid JSON in manifest: {e}")
        except IOError as e:
            raise ManifestError(f"Failed to read manifest: {e}")
        
        return ManifestParser.parse_dict(data, manifest_path)
    
    @staticmethod
    def parse_dict(data: Dict[str, Any], manifest_path: Optional[str] = None) -> PackageManifest:
        """
        Parse manifest data from a dictionary.
        
        Args:
            data: Dictionary containing manifest data
            manifest_path: Optional path to the manifest file
            
        Returns:
            Parsed PackageManifest object
            
        Raises:
            ManifestError: If required fields are missing or invalid
        """
        # Required fields
        if 'name' not in data:
            raise ManifestError("Manifest missing required field: 'name'")
        if 'version' not in data:
            raise ManifestError("Manifest missing required field: 'version'")
        
        name = data['name']
        version = data['version']
        
        if not isinstance(name, str) or not name:
            raise ManifestError("Invalid package name: must be non-empty string")
        if not isinstance(version, str) or not version:
            raise ManifestError("Invalid version: must be non-empty string")
        
        # Parse capabilities
        capabilities_data = data.get('capabilities', {})
        capabilities = CapabilityDeclaration(
            keywords=set(capabilities_data.get('keywords', [])),
            operators=set(capabilities_data.get('operators', [])),
            tags=set(capabilities_data.get('tags', [])),
            macros=set(capabilities_data.get('macros', [])),
            custom_types=set(capabilities_data.get('custom_types', [])),
        )
        
        # Parse extension hooks
        extensions_data = data.get('extensions', {})
        extensions = ExtensionHooks(
            parser_hooks=extensions_data.get('parser_hooks', []),
            semantic_hooks=extensions_data.get('semantic_hooks', []),
            codegen_hooks=extensions_data.get('codegen_hooks', []),
            runtime_hooks=extensions_data.get('runtime_hooks', []),
        )
        
        # Parse dependencies
        dependencies = data.get('dependencies', [])
        if not isinstance(dependencies, list):
            raise ManifestError("Invalid dependencies: must be a list")
        
        # Parse metadata
        metadata = data.get('metadata', {})
        if not isinstance(metadata, dict):
            raise ManifestError("Invalid metadata: must be a dictionary")
        
        # Determine package directory
        package_dir = None
        if manifest_path:
            package_dir = os.path.dirname(manifest_path)
        
        manifest = PackageManifest(
            name=name,
            version=version,
            capabilities=capabilities,
            extensions=extensions,
            dependencies=dependencies,
            metadata=metadata,
            package_dir=package_dir,
            manifest_path=manifest_path,
        )
        
        return manifest
    
    @staticmethod
    def validate_and_parse(manifest_path: str) -> PackageManifest:
        """
        Parse and validate a manifest file in one step.
        
        Args:
            manifest_path: Path to the manifest JSON file
            
        Returns:
            Validated PackageManifest object
            
        Raises:
            ManifestError: If parsing or validation fails
        """
        manifest = ManifestParser.parse_file(manifest_path)
        errors = ManifestValidator.validate_manifest(manifest)
        
        if errors:
            error_msg = f"Manifest validation failed for '{manifest.name}':\n"
            error_msg += "\n".join(f"  - {err}" for err in errors)
            raise ManifestError(error_msg)
        
        return manifest


class ManifestRegistry:
    """
    Global registry for loaded package manifests.
    
    This registry maintains the collection of all loaded package manifests
    and provides methods for querying capabilities across all packages.
    """
    
    def __init__(self):
        self._manifests: Dict[str, PackageManifest] = {}
        self._keywords: Dict[str, str] = {}  # keyword -> package_name
        self._operators: Dict[str, str] = {}  # operator -> package_name
        self._tags: Dict[str, str] = {}  # tag -> package_name
    
    def register(self, manifest: PackageManifest) -> None:
        """
        Register a package manifest in the global registry.
        
        Args:
            manifest: The manifest to register
            
        Raises:
            ManifestError: If there are capability conflicts
        """
        package_name = manifest.name
        
        # Check for conflicts with existing registrations
        for keyword in manifest.capabilities.keywords:
            existing = self._keywords.get(keyword)
            if existing and existing != package_name:
                raise ManifestError(
                    f"Keyword conflict: '{keyword}' is already registered by package '{existing}'"
                )
        
        for operator in manifest.capabilities.operators:
            existing = self._operators.get(operator)
            if existing and existing != package_name:
                raise ManifestError(
                    f"Operator conflict: '{operator}' is already registered by package '{existing}'"
                )
        
        for tag in manifest.capabilities.tags:
            existing = self._tags.get(tag)
            if existing and existing != package_name:
                raise ManifestError(
                    f"Tag conflict: '{tag}' is already registered by package '{existing}'"
                )
        
        # Register the manifest
        self._manifests[package_name] = manifest
        
        # Index capabilities
        for keyword in manifest.capabilities.keywords:
            self._keywords[keyword] = package_name
        
        for operator in manifest.capabilities.operators:
            self._operators[operator] = package_name
        
        for tag in manifest.capabilities.tags:
            self._tags[tag] = package_name
    
    def get_manifest(self, package_name: str) -> Optional[PackageManifest]:
        """Get a manifest by package name."""
        return self._manifests.get(package_name)
    
    def is_loaded(self, package_name: str) -> bool:
        """Check if a package manifest is already loaded."""
        return package_name in self._manifests
    
    def get_all_manifests(self) -> Dict[str, PackageManifest]:
        """Get all registered manifests."""
        return self._manifests.copy()
    
    def get_keywords(self) -> Set[str]:
        """Get all registered keywords."""
        return set(self._keywords.keys())
    
    def get_operators(self) -> Set[str]:
        """Get all registered operators."""
        return set(self._operators.keys())
    
    def get_tags(self) -> Set[str]:
        """Get all registered tags."""
        return set(self._tags.keys())
    
    def get_keyword_owner(self, keyword: str) -> Optional[str]:
        """Get the package name that owns a keyword."""
        return self._keywords.get(keyword)
    
    def get_operator_owner(self, operator: str) -> Optional[str]:
        """Get the package name that owns an operator."""
        return self._operators.get(operator)
    
    def get_tag_owner(self, tag: str) -> Optional[str]:
        """Get the package name that owns a tag."""
        return self._tags.get(tag)
    
    def clear(self) -> None:
        """Clear all registered manifests."""
        self._manifests.clear()
        self._keywords.clear()
        self._operators.clear()
        self._tags.clear()


# Global manifest registry instance
_global_registry = ManifestRegistry()


def get_global_registry() -> ManifestRegistry:
    """Get the global manifest registry instance."""
    return _global_registry


def reset_global_registry() -> None:
    """Reset the global manifest registry (useful for testing)."""
    _global_registry.clear()